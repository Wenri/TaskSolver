"""codex_process — in-process Python side of the codex instrumentation (WIRE_MODULE target).

The patched codex build's embedded worker imports this module and calls
``dispatch(kind, stream_id, data)`` for every capture event: ``codex_request`` (the serialized
``/v1/responses`` request) and ``codex_event`` (one ResponsesStreamEvent). It feeds them to a
correlator that emits a decoded ``codex_turn``. Stdlib-only (same rule as pyagy.agy_process).

The full dispatch/router + mp_child wiring lands in Phase 7; today this package exposes the
Responses decoder (``responses_decode``) so the turn shaping can be unit-tested offline.
"""
