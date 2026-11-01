"""agy_process — in-process Python side of the antigravity instrumentation.

The worker thread (wirecap/native) calls `dispatch(kind, stream_id, data)` for every book
event. This is where your custom logic lives.

Contract:
  dispatch(kind: str, stream_id: int, data: bytes) -> bytes | None
    * ASYNC books (logging): return value is ignored — do fire-and-forget work.
    * The SYNC exit book flushes capture before Go exits. TLS capture is ASYNC;
      the current trampoline does not support outbound request rewriting.

kind values with special handling: "tls_write" is recorded raw AND fed to the
correlator as the model REQUEST (egress); the correlator sniffs each connection and
routes HTTP/1.1 (the model endpoint) vs HTTP/2 (agy's gRPC conns → h2msg events).
"resp_chunk" is the model RESPONSE: agy's SSE parser hands us each decoded `data:` line
(the pull-based transport read has no entry-arg source), which the correlator accumulates
into the `genai_turn` it emits paired with the request. "smoke" prints. Any other kind the
shim emits — "dns", "http_rt", "resp", "serialize", "marshal", "proto_marshal", the
trampoline "send_user_msg"/"stream_send", or a new one — is recorded by the default path
(never silently dropped).

Knobs: AGY_PROC_H2=0 disables HTTP/2 reassembly; WIRE_CORRELATE=0 (legacy
AGY_PROC_CORRELATE) disables the
genai-turn correlator (raw capture only).
"""
from __future__ import annotations

import os
import re
import sys
import struct
import time
import traceback
from collections.abc import Callable, Mapping
from typing import cast, overload

from wirecap.types import Event

from wirecap.decode.record import Recorder
from wirecap.decode import h2reassemble as h2
from wirecap.decode.capture import BaseCorrelator
from .http1sse import GenaiTurnBuilder
from .system_messages import decode_system_message

@overload
def _knob(name: str, default: str) -> str: ...
@overload
def _knob(name: str, default: None = None) -> str | None: ...
def _knob(name: str, default: str | None = None) -> str | None:
    """Read a SHARED decode-layer knob: WIRE_<name>, falling back to the legacy AGY_PROC_<name>.

    These three (CAPTURE / PREVIEW / CORRELATE) configure wirecap.decode objects — the Recorder and
    BaseCorrelator — which pycodex spells WIRE_*, so agy now accepts the same names. They are
    Python-only: the C shim reads AGY_PROC_{ENABLE,LOG,STACK,CONV_ID,REAL_EXE,FORCE,
    CGT_*} and the bridge reads only WIRE_MODULE/WIRE_MAXCOPY, so that split stays as it is."""
    return os.environ.get("WIRE_" + name, os.environ.get("AGY_PROC_" + name, default))


_rec = Recorder(path=_knob("CAPTURE", "agy-capture.jsonl"),
                preview=int(_knob("PREVIEW", "64")))
# AGY_PROC_H2 keeps its name: HTTP/2 reassembly is an agy-only tap (codex hands the bridge
# pre-parsed JSON and never builds a Reassembler), so there is no codex spelling to converge on.
_reasm = h2.Reassembler(_rec) if os.environ.get("AGY_PROC_H2", "1") != "0" else None
_corr = (BaseCorrelator(_rec, GenaiTurnBuilder(), _reasm)
         if _knob("CORRELATE", "1") != "0" else None)


def subscribe(fn: Callable[[Event], object]) -> None:
    """Register an in-process consumer fn(obj) for every recorded/decoded event — used by
    AgyProcess targets (e.g. mp_child.stream_turns) to stream decoded turns home live."""
    _rec.subscribe(fn)


def unsubscribe(fn: Callable[[Event], object]) -> None:
    """Drop a consumer registered with :func:`subscribe` — a finished mp_child target calls this
    so the recorder stops feeding a queue nobody drains."""
    _rec.unsubscribe(fn)

def on_tls_write(stream_id: int, data: bytes) -> None:
    _rec.record("tls_write", stream_id, data)
    if _corr:
        _corr.feed("c2s", stream_id, data, time.time())


def _response_payload(source: str, stream_id: int, data: bytes) -> bytes | None:
    """Validate a native packet before it reaches the legacy raw-payload handler."""
    original = None
    if len(data) < 16:
        error = "incomplete response capture header"
    else:
        version, original = struct.unpack_from("<IQ", data, 4)
        if data[:4] != b"AGTX":
            error = "invalid response capture header"
        elif version != 1:
            error = "unsupported response capture version"
        elif len(data) - 16 != original:
            error = "response capture size mismatch"
        else:
            return data[16:]
    _rec.event({"kind": "response_capture_error", "capture_source": source,
                "stream": stream_id, "error": error, "original_bytes": original,
                "captured_bytes": max(0, len(data) - 16)})
    return None


def on_tls_read(stream_id: int, data: bytes) -> None:
    _rec.record("tls_read", stream_id, data)
    if _corr:
        _corr.feed("s2c", stream_id, data, time.time())
    return None


def on_resp_chunk(stream_id: int, data: bytes) -> None:
    # The wire RESPONSE: one decoded SSE `data:` line per fire (toStreamResponseChunk).
    # Hand it to the correlator, which accumulates the stream and emits a genai_turn at
    # the terminal event. stream_id is the Go string pointer (not a connection id) — agy
    # overlaps model and title turns, so the decoder keys them by responseId.
    if _corr:
        _corr.feed_chunk(data, time.time())
    return None


def on_http_rt(stream_id: int, data: bytes) -> None:
    _rec.record("http_rt", stream_id, data)
    return None


def on_dns(stream_id: int, data: bytes) -> None:
    _rec.record("dns", stream_id, data)
    return None


def on_smoke(stream_id: int, data: bytes) -> None:
    print("[agy_process] smoke book fired — end-to-end pipeline OK", file=sys.stderr, flush=True)
    _rec.record("smoke", stream_id, data)
    return None


def on_exit(stream_id: int, data: bytes) -> None:
    # os.Exit(code) fired — the clean end-of-capture marker. The exit code rode stream_id from the
    # C book, which SYNC-emits so this record is written (line-buffered → flushed) BEFORE agy's
    # exit_group syscall. A capture that ends without this marker was truncated (crash/kill).
    # Responses still streaming (e.g. a session-title call agy never waited for) are emitted
    # first: they accumulate per responseId now, so no later request flushes them.
    if _corr:
        _corr.flush()
    _rec.event({"kind": "exit", "code": int(stream_id)})
    return None


# conversation-id capture (AGY_PROC_CONV_ID): the file_open book (os.OpenFile) is C-filtered to
# conversation-store paths, so `data` is a path like `.../conversations/<uuid>.db` or
# `.../brain/<uuid>/.../transcript.jsonl`. Extract the uuid and emit a `conversation_id` event
# (deduped per process; the FIRST distinct id is the main/top-level conversation — subagents open
# their own dirs later). This is the exact, in-process id that agy doesn't expose via env.
_CONV_UUID = re.compile(
    r"(?:conversations/|/brain/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_seen_conv_ids: set[str] = set()


def on_file_open(stream_id: int, data: bytes) -> None:
    m = _CONV_UUID.search(data.decode("utf-8", "replace"))
    if m:
        cid = m.group(1)
        if cid not in _seen_conv_ids:
            _seen_conv_ids.add(cid)
            _rec.event({"kind": "conversation_id", "id": cid})
    return None


def on_readlink_filter(stream_id: int, data: bytes) -> None:
    # The readlink_filter book (os.readlink) RETURN-substituted "/proc/self/exe" — which resolves to
    # the host — with its launch path (`data`, from AGY_PROC_REAL_EXE), so
    # os.Executable()/self-re-exec use the selected host.
    _rec.event({"kind": "readlink_filter", "requested": "/proc/self/exe",
                "substitute": data.decode("utf-8", "replace")})
    return None


def on_cgt_args(stream_id: int, data: bytes) -> None:
    # Diagnostic (AGY_PROC_CGT_ARGS): the C book already rendered a readable arg
    # report; print it in full (the raw recorder would truncate to the preview len)
    # and store the whole text so it survives in the capture JSONL.
    report = data.decode("utf-8", "replace")
    print("[cgt_args] " + report.replace("\n", "\n[cgt_args] "), file=sys.stderr, flush=True)
    _rec.event({"kind": "cgt_args", "stream": stream_id, "report": report})
    return None


def on_callstack(stream_id: int, data: bytes) -> None:
    # AGY_PROC_STACK: `data` = source-book kind (NUL-terminated) + packed u64 frame
    # link-vaddrs (pc - module_base). Record raw; symbolize offline via symbolize.py
    # (the funcmap is too big to load inside agy).
    import struct
    nul = data.find(b"\0")
    src = data[:nul].decode("utf-8", "replace") if nul >= 0 else "?"
    raw = data[nul + 1:] if nul >= 0 else b""
    m = len(raw) // 8
    frames = list(cast(tuple[int, ...], struct.unpack("<%dQ" % m, raw[:m * 8]))) if m else []
    _rec.event({"kind": "callstack", "src": src, "frames": frames})
    return None


def _on_model_text(kind: str) -> Callable[[int, bytes], None]:
    # model-text leaf getters: `data` is the streamed assistant-text delta (a Go string).
    # Store the FULL text (the raw recorder would truncate to the preview len).
    def handler(stream_id: int, data: bytes) -> None:
        _rec.event({"kind": kind, "stream": stream_id,
                    "text": data.decode("utf-8", "replace")})
        return None
    return handler


def _on_response_packet(source: str) -> Callable[[int, bytes], None]:
    consume = on_resp_chunk if source == "resp_chunk" else _on_model_text(source)

    def handler(stream_id: int, data: bytes) -> None:
        payload = _response_payload(source, stream_id, data)
        if payload is not None:
            consume(stream_id, payload)
    return handler


def _on_system_message(source: str) -> Callable[[int, bytes], None]:
    def handler(step_address: int, data: bytes) -> None:
        try:
            event: Mapping[str, object] = decode_system_message(source, step_address, data)
        except ValueError as exc:
            event = {"kind": "system_message_capture_error", "capture_source": source,
                     "step_address": step_address, "error": str(exc), "captured_bytes": len(data)}
        _rec.event(event)
        return None
    return handler


_ROUTER: dict[str, Callable[[int, bytes], bytes | None]] = {
    "tls_write": on_tls_write,
    "tls_read": on_tls_read,
    "resp_chunk": on_resp_chunk,
    "resp_chunk_packet": _on_response_packet("resp_chunk"),
    "app_response_packet": _on_response_packet("app_response"),
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
    # Assembled assistant answer at updateWithStep, validated before text decoding.
    "app_response": _on_model_text("app_response"),
    "system_message": _on_system_message("system_message"),
    "system_message_legacy": _on_system_message("system_message_legacy"),
}


def dispatch(kind: str, stream_id: int, data: bytes) -> bytes | None:
    try:
        handler = _ROUTER.get(kind)
        if handler is not None:
            return handler(stream_id, data)
        # Unrouted kind (resp/serialize/marshal/proto_marshal/send_user_msg/…):
        # record it rather than drop it, so a book is never silently ineffective.
        _rec.record(kind, stream_id, data)
        return None
    except Exception:
        traceback.print_exc()
        return None


# AgyProcess embedded-worker channel: run the pickled target on a daemon thread — separate from this
# dispatch worker so a blocking recv() there can't starve book dispatch. The capture pipeline above
# stays live, so the target can consume decoded events in-process AND stream results over the
# Connection. start() self-gates on the boot fd + sys._wire_shim and never raises.
from wirecap.decode import mp_child
mp_child.start()
