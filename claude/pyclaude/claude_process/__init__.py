"""claude_process — in-process Python side of the Claude Code instrumentation (the WIRE_MODULE
target).

Claude Code's bridged runtime loads ``wirecap_node.node`` when ``WIRE_ENABLE`` is set,
imports this module on the embedded CPython worker thread, and calls
``dispatch(kind, stream_id, data)`` for every event ``claude/native_bridge.mjs`` emits:

  * ``claude_request`` — the serialized ``/v1/messages`` request JSON (one per model call).
  * ``claude_event``   — one parsed SSE event JSON (or a non-streaming ``message`` JSON).
  * ``claude_end``     — stream iterator finished or closed early.

They feed a correlator that pairs the request with its stream events by ``stream_id``
and emits a decoded ``claude_turn`` (see :mod:`claude_decode`). Everything is recorded
to the ``WIRE_CAPTURE`` JSONL.

Stdlib-only (same rule as pyagy.agy_process / pycodex.codex_process / pykimi.kimi_process):
loaded by the embedded libpython, which resolves this module from its own env's
site-packages (``site`` runs; ``PYTHONHOME`` selects the env) — never import ``tasksolver``
or a non-stdlib package here.
"""
from __future__ import annotations

import os
import time
import traceback
from collections.abc import Callable

from wirecap.decode.record import Recorder
from wirecap.types import Event, json_loads_object

from .claude_decode import ClaudeCorrelator

_rec = Recorder(path=os.environ.get("WIRE_CAPTURE", "claude-capture.jsonl"),
                preview=int(os.environ.get("WIRE_PREVIEW", "64")))
_corr = (ClaudeCorrelator(_rec)
         if os.environ.get("WIRE_CORRELATE", "1") != "0" else None)


def subscribe(fn: Callable[[Event], object]) -> None:
    """Register an in-process consumer fn(obj) for every recorded/decoded event."""
    _rec.subscribe(fn)


def unsubscribe(fn: Callable[[Event], object]) -> None:
    """Drop a consumer registered with :func:`subscribe`."""
    _rec.unsubscribe(fn)


def on_claude_request(stream_id: int, data: bytes) -> None:
    _rec.record("claude_request", stream_id, data)
    if _corr:
        try:
            req = json_loads_object(data)
        except (ValueError, TypeError):
            req = None
        _corr.request(stream_id, req, time.time())
    return None


def on_claude_event(stream_id: int, data: bytes) -> None:
    if _corr:
        _corr.feed_chunk(stream_id, data, time.time())
    return None


def on_claude_end(stream_id: int, _data: bytes) -> None:
    if _corr:
        _corr.finish(stream_id)
    return None


_ROUTER: dict[str, Callable[[int, bytes], bytes | None]] = {
    "claude_request": on_claude_request,
    "claude_event": on_claude_event,
    "claude_end": on_claude_end,
}


def dispatch(kind: str, stream_id: int, data: bytes) -> bytes | None:
    try:
        handler = _ROUTER.get(kind)
        if handler is not None:
            return handler(stream_id, data)
        _rec.record(kind, stream_id, data)
        return None
    except Exception:
        traceback.print_exc()
        return None


from wirecap.decode import mp_child
mp_child.start()
