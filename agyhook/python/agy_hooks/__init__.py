"""agy_hooks — in-process Python side of the agyhook instrumentation.

The native shim's worker thread calls `dispatch(kind, stream_id, data)` for every
hook event (see native/pybridge.c). This is where your custom logic lives.

Contract:
  dispatch(kind: str, stream_id: int, data: bytes) -> bytes | None
    * ASYNC hooks (logging): return value is ignored — do fire-and-forget work.
    * SYNC hooks (modify, e.g. tls_write when AGY_HOOK_TLS_WRITE_SYNC=1): return
      replacement bytes (must be <= original length for in-place rewrite) or None
      to leave unchanged. Keep SYNC handlers fast/CPU-bound (they block a Go
      goroutine — see README's GC-stall note).

kind values: "smoke" | "tls_write" | "tls_read" | "http_rt" | "dns".
Set AGY_HOOK_H2=0 to disable HTTP/2 reassembly (raw capture only).
"""
import os
import sys
import traceback

from .record import Recorder
from . import h2reassemble as h2

_rec = Recorder()
_reasm = h2.Reassembler(_rec) if os.environ.get("AGY_HOOK_H2", "1") != "0" else None


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
    print("[agy_hooks] smoke hook fired — end-to-end pipeline OK", file=sys.stderr, flush=True)
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
        return handler(stream_id, data) if handler else None
    except Exception:
        traceback.print_exc()
        return None
