"""agy_process — in-process Python side of the antigravity instrumentation.

The native shim's worker thread calls `dispatch(kind, stream_id, data)` for every
hook event (see src/pybridge.c). This is where your custom logic lives.

Contract:
  dispatch(kind: str, stream_id: int, data: bytes) -> bytes | None
    * ASYNC hooks (logging): return value is ignored — do fire-and-forget work.
    * SYNC hooks (modify, e.g. tls_write when AGY_PROC_TLS_WRITE_SYNC=1): return
      replacement bytes (must be <= original length for in-place rewrite) or None
      to leave unchanged. Keep SYNC handlers fast/CPU-bound (they block a Go
      goroutine — see README's GC-stall note).

kind values with special handling: "tls_write"/"tls_read" are recorded raw AND fed
to the correlator, which sniffs each connection and routes HTTP/1.1 (the model
endpoint → genai_turn events) vs HTTP/2 (agy's gRPC conns → h2msg events). "smoke"
prints. Any other kind the shim emits — "dns", "http_rt", "resp", "serialize",
"marshal", "proto_marshal", the stage-8 "send_user_msg"/"stream_send", the stage-9
"cgt_getenv", or a new one — is recorded by the default path (never silently dropped).

Knobs: AGY_PROC_H2=0 disables HTTP/2 reassembly; AGY_PROC_CORRELATE=0 disables the
genai-turn correlator (raw capture only).
"""
import os
import sys
import time
import traceback

from .record import Recorder
from . import h2reassemble as h2
from . import capture

_rec = Recorder()
_reasm = h2.Reassembler(_rec) if os.environ.get("AGY_PROC_H2", "1") != "0" else None
_corr = (capture.Correlator(_rec, _reasm)
         if os.environ.get("AGY_PROC_CORRELATE", "1") != "0" else None)

# SYNC egress rewrite registry (task #8). Lazily bound to keep import cheap and pure.
_rewrite = None


def _rewrite_egress(stream_id, data):
    """Return replacement bytes (<= len(data)) for a model request, or None."""
    global _rewrite
    if _rewrite is None:
        if not (os.environ.get("AGY_PROC_REWRITE_RULES") or os.environ.get("AGY_PROC_REWRITE")):
            return None
        from . import rewrite as _rw
        _rewrite = _rw.RewriteRegistry.from_env()
    return _rewrite.rewrite(stream_id, data)


def on_tls_write(stream_id, data):
    _rec.record("tls_write", stream_id, data)
    if _corr:
        _corr.feed("c2s", stream_id, data, time.time())
    return _rewrite_egress(stream_id, data)  # SYNC rewrite point (None unless configured)


def on_tls_read(stream_id, data):
    _rec.record("tls_read", stream_id, data)
    if _corr:
        _corr.feed("s2c", stream_id, data, time.time())
    return None


def on_http_rt(stream_id, data):
    _rec.record("http_rt", stream_id, data)
    return None


def on_dns(stream_id, data):
    _rec.record("dns", stream_id, data)
    return None


def on_smoke(stream_id, data):
    print("[agy_process] smoke hook fired — end-to-end pipeline OK", file=sys.stderr, flush=True)
    _rec.record("smoke", stream_id, data)
    return None


_ROUTER = {
    "tls_write": on_tls_write,
    "tls_read": on_tls_read,
    "http_rt": on_http_rt,
    "dns": on_dns,
    "smoke": on_smoke,
}


def dispatch(kind, stream_id, data):
    try:
        handler = _ROUTER.get(kind)
        if handler is not None:
            return handler(stream_id, data)
        # Unrouted kind (resp/serialize/marshal/proto_marshal/send_user_msg/…):
        # record it rather than drop it, so a hook is never silently ineffective.
        _rec.record(kind, stream_id, data)
        return None
    except Exception:
        traceback.print_exc()
        return None
