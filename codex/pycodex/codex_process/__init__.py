"""codex_process — in-process Python side of the codex instrumentation (the WIRE_MODULE target).

The patched codex build's embedded worker (wirecap/native) imports this module and calls
``dispatch(kind, stream_id, data)`` for every capture event the Rust ``codex_wirecap`` shim emits:

  * ``codex_request`` — the serialized ``/v1/responses`` request JSON (one per turn).
  * ``codex_event``   — one raw ``ResponsesStreamEvent`` JSON (many per turn; same on SSE + WS).

They feed a correlator that pairs the request with its stream events and emits a decoded
``codex_turn`` (see :mod:`responses_decode`). Everything is recorded to the ``WIRE_CAPTURE`` JSONL.

Stdlib-only (same rule as pyagy.agy_process): loaded by codex's embedded libpython, which resolves
this module from its own env's site-packages (``site`` runs; ``PYTHONHOME`` selects the env) — never
import ``tasksolver`` or a non-stdlib package here (the shared decode layer it uses, ``wirecap.decode``,
is itself pure).
"""
import json
import os
import sys
import time
import traceback

from wirecap.decode.record import Recorder
from wirecap.decode.capture import BaseCorrelator

from .responses_decode import ResponsesTurnBuilder

_rec = Recorder(path=os.environ.get("WIRE_CAPTURE", "codex-capture.jsonl"),
                preview=int(os.environ.get("WIRE_PREVIEW", "64")))
_corr = (BaseCorrelator(_rec, ResponsesTurnBuilder())
         if os.environ.get("WIRE_CORRELATE", "1") != "0" else None)


def subscribe(fn):
    """Register an in-process consumer fn(obj) for every recorded/decoded event — used by a
    CodexProcess target (mp_child.stream_turns) to stream decoded turns home live."""
    _rec.subscribe(fn)


def on_codex_request(stream_id, data):
    # The model REQUEST (/v1/responses body). Record raw (metadata) + hand the parsed JSON to the
    # correlator so the decoded codex_turn.request reflects what was sent.
    _rec.record("codex_request", stream_id, data)
    if _corr:
        try:
            req = json.loads(data)
        except ValueError:
            req = None
        _corr.feed_request(req, time.time(), stream_id=stream_id)
    return None


def on_codex_event(stream_id, data):
    # One streamed ResponsesStreamEvent (byte-identical on the SSE + WebSocket transports). Accumulate
    # into the in-flight turn; the correlator emits the codex_turn at the terminal (response.completed)
    # event, paired with the pending request.
    if _corr:
        _corr.feed_events(_corr._builder.parse_events(data), time.time())
    return None


_ROUTER = {
    "codex_request": on_codex_request,
    "codex_event": on_codex_event,
}


def dispatch(kind, stream_id, data):
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


# CodexProcess embedded-worker channel: when the boot pipe is wired (WIRE_MP_BOOT_FD set + this is
# the codex-embedded interpreter), run the pickled target on a daemon thread so it can stream decoded
# turns home over the result queue. mp_child is shared (wirecap.decode); a no-op if the fd is absent
# (e.g. a bare `codex exec` smoke run, which just writes the WIRE_CAPTURE JSONL).
if os.environ.get("WIRE_MP_BOOT_FD") and getattr(sys, "_wire_shim", False):
    try:
        from wirecap.decode import mp_child
        mp_child.start()
    except Exception:
        traceback.print_exc()
