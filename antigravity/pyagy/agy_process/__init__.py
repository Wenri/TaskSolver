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
        _rewrite = _rw.RewriteRegistry.from_env(_rec)
    return _rewrite.rewrite(stream_id, data)


def on_tls_write(stream_id, data):
    out = _rewrite_egress(stream_id, data)   # SYNC rewrite point (None unless configured)
    _rec.record("tls_write", stream_id, data)
    if _corr:
        # Feed the correlator what actually goes on the wire (post-rewrite), so a
        # decoded genai_turn.request reflects the request as sent, not as authored.
        _corr.feed("c2s", stream_id, out if out is not None else data, time.time())
    return out


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


def on_cgt_args(stream_id, data):
    # Diagnostic (AGY_PROC_CGT_ARGS): the C hook already rendered a readable arg
    # report; print it in full (the raw recorder would truncate to the preview len)
    # and store the whole text so it survives in the capture JSONL.
    report = data.decode("utf-8", "replace")
    print("[cgt_args] " + report.replace("\n", "\n[cgt_args] "), file=sys.stderr, flush=True)
    _rec.event({"kind": "cgt_args", "stream": stream_id, "report": report})
    return None


def on_callstack(stream_id, data):
    # AGY_PROC_STACK: `data` = source-hook kind (NUL-terminated) + packed u64 frame
    # link-vaddrs (pc - module_base). Record raw; symbolize offline via symbolize.py
    # (the funcmap is too big to load inside agy).
    import struct
    nul = data.find(b"\0")
    src = data[:nul].decode("utf-8", "replace") if nul >= 0 else "?"
    raw = data[nul + 1:] if nul >= 0 else b""
    m = len(raw) // 8
    frames = list(struct.unpack("<%dQ" % m, raw[:m * 8])) if m else []
    _rec.event({"kind": "callstack", "src": src, "frames": frames})
    return None


def _on_model_text(kind):
    # stage-11 leaf getters: `data` is the streamed assistant-text delta (a Go string).
    # Store the FULL text (the raw recorder would truncate to the preview len).
    def handler(stream_id, data):
        _rec.event({"kind": kind, "stream": stream_id,
                    "text": data.decode("utf-8", "replace")})
        return None
    return handler


_ROUTER = {
    "tls_write": on_tls_write,
    "tls_read": on_tls_read,
    "http_rt": on_http_rt,
    "dns": on_dns,
    "smoke": on_smoke,
    "cgt_args": on_cgt_args,
    "callstack": on_callstack,
    "delta_ccpa": _on_model_text("delta_ccpa"),
    "delta_completion": _on_model_text("delta_completion"),
    "resp_text": _on_model_text("resp_text"),
    "resp_thinking": _on_model_text("resp_thinking"),
    "resp_view": _on_model_text("resp_view"),
    # Plan 7: the assembled assistant answer decoded at the shallow consumer boundary
    # (updateWithStep, rsi+0x8) — the app-boundary RESPONSE. Stored full (not preview-
    # truncated) so the client can prefer it over the wire-reassembled genai_turn.text.
    "app_response": _on_model_text("app_response"),
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
