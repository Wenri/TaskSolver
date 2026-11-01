"""codex_process — in-process Python side of the codex instrumentation (the WIRE_MODULE target).

The patched codex build's embedded worker (wirecap/native) imports this module and calls
``dispatch(kind, stream_id, data)`` for every capture event the Rust ``codex_wirecap`` shim emits:

  * ``codex_request`` — the serialized ``/v1/responses`` request JSON (one per turn).
  * ``codex_event``   — one raw ``ResponsesStreamEvent`` JSON (many per turn; same on SSE + WS).
  * ``codex_end``     — transport closed, including errors or cancellation.

They feed a correlator that pairs the request with its stream events and emits a decoded
``codex_turn`` (see :mod:`responses_decode`). Everything is recorded to the ``WIRE_CAPTURE`` JSONL.

Stdlib-only (same rule as pyagy.agy_process): loaded by codex's embedded libpython, which resolves
this module from its own env's site-packages (``site`` runs; ``PYTHONHOME`` selects the env) — never
import ``tasksolver`` or a non-stdlib package here (the shared decode layer it uses, ``wirecap.decode``,
is itself pure).
"""
from __future__ import annotations

import os
import time
import traceback
from collections.abc import Callable

from wirecap.types import Event, json_loads_object

from wirecap.decode.record import Recorder
from .responses_decode import ResponsesCorrelator

_rec = Recorder(path=os.environ.get("WIRE_CAPTURE", "codex-capture.jsonl"),
                preview=int(os.environ.get("WIRE_PREVIEW", "64")))
_corr = (ResponsesCorrelator(_rec)
         if os.environ.get("WIRE_CORRELATE", "1") != "0" else None)


def subscribe(fn: Callable[[Event], object]) -> None:
    """Register an in-process consumer fn(obj) for every recorded/decoded event — used by a
    CodexProcess target (mp_child.stream_turns) to stream decoded turns home live."""
    _rec.subscribe(fn)


def unsubscribe(fn: Callable[[Event], object]) -> None:
    """Drop a consumer registered with :func:`subscribe` — a finished mp_child target calls this
    so the recorder stops feeding a queue nobody drains."""
    _rec.unsubscribe(fn)


def on_codex_request(stream_id: int, data: bytes) -> None:
    # The model REQUEST (/v1/responses body). Record raw (metadata) + hand the parsed JSON to the
    # correlator so the decoded codex_turn.request reflects what was sent.
    _rec.record("codex_request", stream_id, data)
    if _corr:
        try:
            req = json_loads_object(data)
        except (ValueError, TypeError):
            req = None
        _corr.request(stream_id, req, time.time())
    return None


def on_codex_event(stream_id: int, data: bytes) -> None:
    # One streamed ResponsesStreamEvent (byte-identical on the SSE + WebSocket transports). Accumulate
    # into the in-flight turn; the correlator emits the codex_turn at the terminal (response.completed)
    # event, paired with the pending request.
    if _corr:
        _corr.feed_chunk(stream_id, data, time.time())
    return None


def on_codex_end(stream_id: int, _data: bytes) -> None:
    if _corr:
        _corr.finish(stream_id)


_ROUTER: dict[str, Callable[[int, bytes], bytes | None]] = {
    "codex_request": on_codex_request,
    "codex_event": on_codex_event,
    "codex_end": on_codex_end,
}


def dispatch(kind: str, stream_id: int, data: bytes) -> bytes | None:
    try:
        handler = _ROUTER.get(kind)
        if handler is not None:
            return handler(stream_id, data)
        # Unknown kind: record it rather than drop it, so a new emit is never silently ineffective.
        _rec.record(kind, stream_id, data)
        return None
    except Exception:
        traceback.print_exc()
        return None


# CodexProcess embedded-worker channel: run the pickled target on a daemon thread so it can stream
# decoded turns home over the result queue. start() (shared, wirecap.decode) self-gates on the boot
# fd + sys._wire_shim, so a bare `codex exec` smoke run — which just writes the WIRE_CAPTURE JSONL —
# is a no-op.
from wirecap.decode import mp_child
mp_child.start()
