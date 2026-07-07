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

kind values with special handling: "tls_write" is recorded raw AND fed to the
correlator as the model REQUEST (egress); the correlator sniffs each connection and
routes HTTP/1.1 (the model endpoint) vs HTTP/2 (agy's gRPC conns → h2msg events).
"resp_chunk" is the model RESPONSE: agy's SSE parser hands us each decoded `data:` line
(the pull-based transport read has no entry-arg source), which the correlator accumulates
into the `genai_turn` it emits paired with the request. "smoke" prints. Any other kind the
shim emits — "dns", "http_rt", "resp", "serialize", "marshal", "proto_marshal", the
trampoline "send_user_msg"/"stream_send", or a new one — is recorded by the default path
(never silently dropped).

Knobs: AGY_PROC_H2=0 disables HTTP/2 reassembly; AGY_PROC_CORRELATE=0 disables the
genai-turn correlator (raw capture only).
"""
import os
import re
import sys
import time
import traceback

from wirecap.decode.record import Recorder
from wirecap.decode import h2reassemble as h2
from . import capture

_rec = Recorder(path=os.environ.get("AGY_PROC_CAPTURE", "agy-capture.jsonl"),
                preview=int(os.environ.get("AGY_PROC_PREVIEW", "64")))
_reasm = h2.Reassembler(_rec) if os.environ.get("AGY_PROC_H2", "1") != "0" else None
_corr = (capture.Correlator(_rec, _reasm)
         if os.environ.get("AGY_PROC_CORRELATE", "1") != "0" else None)


def subscribe(fn):
    """Register an in-process consumer fn(obj) for every recorded/decoded event — used by
    AgyProcess targets (e.g. mp_child.stream_turns) to stream decoded turns home live."""
    _rec.subscribe(fn)

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


def on_resp_chunk(stream_id, data):
    # The wire RESPONSE: one decoded SSE `data:` line per fire (toStreamResponseChunk).
    # Hand it to the correlator, which accumulates the stream and emits a genai_turn at
    # the terminal event. stream_id is the Go string pointer (not a connection id) — agy
    # runs one model turn at a time, so the correlator keys off arrival order, not id.
    if _corr:
        _corr.feed_resp_chunk(data, time.time())
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


def on_exit(stream_id, data):
    # os.Exit(code) fired — the clean end-of-capture marker. The exit code rode stream_id from the
    # C hook, which SYNC-emits so this record is written (line-buffered → flushed) BEFORE agy's
    # exit_group syscall. A capture that ends without this marker was truncated (crash/kill).
    _rec.event({"kind": "exit", "code": int(stream_id)})
    return None


# conversation-id capture (AGY_PROC_CONV_ID): the FILE_OPEN hook (os.OpenFile) is C-filtered to
# conversation-store paths, so `data` is a path like `.../conversations/<uuid>.db` or
# `.../brain/<uuid>/.../transcript.jsonl`. Extract the uuid and emit a `conversation_id` event
# (deduped per process; the FIRST distinct id is the main/top-level conversation — subagents open
# their own dirs later). This is the exact, in-process id that agy doesn't expose via env.
_CONV_UUID = re.compile(
    r"(?:conversations/|/brain/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_seen_conv_ids = set()


def on_file_open(stream_id, data):
    m = _CONV_UUID.search(data.decode("utf-8", "replace"))
    if m:
        cid = m.group(1)
        if cid not in _seen_conv_ids:
            _seen_conv_ids.add(cid)
            _rec.event({"kind": "conversation_id", "id": cid})
    return None


def on_readlink_filter(stream_id, data):
    # The READLINK_FILTER hook (os.readlink) RETURN-substituted "/proc/self/exe" — which resolves to
    # the loader under `ld.so --preload` — with the real agy path (`data`, from AGY_PROC_REAL_EXE), so
    # os.Executable()/self-re-exec see agy, not ld.so. `data` is the substitute path we handed back.
    _rec.event({"kind": "readlink_filter", "requested": "/proc/self/exe",
                "substitute": data.decode("utf-8", "replace")})
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
    # model-text leaf getters: `data` is the streamed assistant-text delta (a Go string).
    # Store the FULL text (the raw recorder would truncate to the preview len).
    def handler(stream_id, data):
        _rec.event({"kind": kind, "stream": stream_id,
                    "text": data.decode("utf-8", "replace")})
        return None
    return handler


_ROUTER = {
    "tls_write": on_tls_write,
    "tls_read": on_tls_read,
    "resp_chunk": on_resp_chunk,
    "http_rt": on_http_rt,
    "dns": on_dns,
    "smoke": on_smoke,
    "exit": on_exit,
    "file_open": on_file_open,
    "readlink_filter": on_readlink_filter,
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


# AgyProcess: when agy is launched with the embedded-worker channel wired (the boot pipe fd is
# exported), run the pickled target on a daemon thread — separate from this dispatch worker so a
# blocking recv() there can't starve hook dispatch. The capture pipeline above stays live, so the
# target can consume decoded events in-process AND stream results over the Connection.
if os.environ.get("AGY_MP_BOOT_FD") and getattr(sys, "is_agy_shim", False):
    try:
        from . import mp_child
        mp_child.start()
    except Exception:
        traceback.print_exc()
