"""kimi_process — in-process Python side of the kimi-code instrumentation (the WIRE_MODULE
target).

The wiretap-patched kimi-code's embedded worker (wirecap/native, hosted by the
``wirecap_node.node`` N-API addon) imports this module and calls
``dispatch(kind, stream_id, data)`` for every event the vendored patch emits:

  * ``kimi_request`` — the full ``LLMRequestLogInput`` JSON (one per model request; carries the
    normalized system prompt + tools + messages).
  * ``kimi_event``   — one ``ModelRequestEvent`` JSON (``part``/``usage``/``finish``/``timing``;
    many per request).
  * ``kimi_wire``    — one persisted wire-journal record ``{scope, record}`` (the same typed
    records kimi-code appends to its per-agent ``wire.jsonl``), recorded raw for the capture.

Request + events feed a correlator that pairs them and emits a decoded ``kimi_turn``
(see :mod:`kimi_decode`). Everything is recorded to the ``WIRE_CAPTURE`` JSONL.

Stdlib-only (same rule as pyagy.agy_process / pycodex.codex_process): loaded by the embedded
libpython, which resolves this module from its own env's site-packages (``site`` runs;
``PYTHONHOME`` selects the env) — never import ``tasksolver`` or a non-stdlib package here.
"""
import json
import os
import sys
import time
import traceback

from wirecap.decode.record import Recorder
from wirecap.decode.capture import BaseCorrelator

from .kimi_decode import KimiTurnBuilder

_rec = Recorder(path=os.environ.get("WIRE_CAPTURE", "kimi-capture.jsonl"),
                preview=int(os.environ.get("WIRE_PREVIEW", "64")))
_corr = (BaseCorrelator(_rec, KimiTurnBuilder())
         if os.environ.get("WIRE_CORRELATE", "1") != "0" else None)


def subscribe(fn):
    """Register an in-process consumer fn(obj) for every recorded/decoded event — used by a
    KimiProcess target (mp_child.stream_turns) to stream decoded turns home live."""
    _rec.subscribe(fn)


def unsubscribe(fn):
    """Drop a consumer registered with :func:`subscribe` — a finished mp_child target calls this
    so the recorder stops feeding a queue nobody drains."""
    _rec.unsubscribe(fn)


def on_kimi_request(stream_id, data):
    # The model request (LLMRequestLogInput). Record raw (metadata) + hand the parsed JSON to the
    # correlator so the decoded kimi_turn.request reflects what was sent.
    _rec.record("kimi_request", stream_id, data)
    if _corr:
        try:
            req = json.loads(data)
        except ValueError:
            req = None
        _corr.feed_request(req, time.time(), stream_id=stream_id)
    return None


def on_kimi_event(stream_id, data):
    # One streamed ModelRequestEvent. Accumulate into the in-flight turn; the correlator emits
    # the kimi_turn at the terminal (finish) event, paired with the pending request.
    if _corr:
        _corr.feed_chunk(data, time.time())
    return None


def on_kimi_wire(stream_id, data):
    # One persisted wire-journal record. Recorded raw only — the turn decode rides the
    # request/event pair; the journal line is capture-side context (usage.record, turn.*, ...).
    _rec.record("kimi_wire", stream_id, data)
    return None


_ROUTER = {
    "kimi_request": on_kimi_request,
    "kimi_event": on_kimi_event,
    "kimi_wire": on_kimi_wire,
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


# KimiProcess embedded-worker channel: run the pickled target on a daemon thread so it can stream
# decoded turns home over the result queue. start() (shared, wirecap.decode) self-gates on the boot
# fd + sys._wire_shim, so a bare `kimi -p` smoke run — which just writes the WIRE_CAPTURE JSONL —
# is a no-op.
from wirecap.decode import mp_child
mp_child.start()
