"""agy's genai correlator — the shared ``BaseCorrelator`` wired to the genai turn shape.

The provider-neutral machinery (HTTP/1.1+SSE framing, HTTP/2 routing, request/response
pairing by time) lives in ``wirecap.decode.capture.BaseCorrelator``; this subclass just
supplies the ``GenaiTurnBuilder`` and agy's response-chunk entry point.

The request side (egress) comes off the wire via the ``tls_write`` entry-arg hook and is
framed by ``feed("c2s", ...)``. The response side does *not*: HTTP/1.1 SSE is pull-based, so
the decrypted inbound bytes are a return value with no entry-arg source (the old ``tls_read``
leave hook that fed s2c is retired — it destabilized agy's GC). Instead agy's own SSE parser,
``toStreamResponseChunk``, hands us each decoded ``data:`` line; ``feed_resp_chunk`` accumulates
them and the base emits the ``genai_turn`` at the terminal (``finishReason``) event, paired
with the pending request.
"""
from wirecap.decode.capture import BaseCorrelator

from .http1sse import GenaiTurnBuilder


class Correlator(BaseCorrelator):
    def __init__(self, recorder, reassembler=None):
        super().__init__(recorder, GenaiTurnBuilder(), reassembler)

    def feed_resp_chunk(self, data, t):
        """Accumulate one decoded SSE response line from agy's ``toStreamResponseChunk`` hook
        (the wire response's only entry-arg source — see module docstring)."""
        self.feed_events(self._builder.parse_events(data), t)
