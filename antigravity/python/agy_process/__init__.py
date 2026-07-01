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

kind values with special handling: "tls_write"/"tls_read" (also fed to HTTP/2
reassembly), "smoke" (prints). Any other kind the shim emits — "dns", "http_rt",
"resp", "serialize", "marshal", "proto_marshal", or a new one — is recorded by the
default path (never silently dropped).
Set AGY_PROC_H2=0 to disable HTTP/2 reassembly (raw capture only).
"""
import os
import sys
import traceback

from .record import Recorder
from . import h2reassemble as h2

_rec = Recorder()
_reasm = h2.Reassembler(_rec) if os.environ.get("AGY_PROC_H2", "1") != "0" else None


def on_tls_write(stream_id, data):
    _rec.record("tls_write", stream_id, data)
    if _reasm:
        _reasm.feed(stream_id, "c2s", data)
    return None  # return bytes here (<= len(data)) to rewrite egress in SYNC mode


def on_tls_read(stream_id, data):
    _rec.record("tls_read", stream_id, data)
    if _reasm:
        _reasm.feed(stream_id, "s2c", data)
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
        # Unrouted kind (resp/serialize/marshal/proto_marshal/…): record it rather
        # than drop it, so stage-4/5 hooks aren't silently ineffective.
        _rec.record(kind, stream_id, data)
        return None
    except Exception:
        traceback.print_exc()
        return None
