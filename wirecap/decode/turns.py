"""TurnBuilder — the provider-specific shaping the correlator delegates to.

A ``BaseCorrelator`` (capture.py) handles the provider-*neutral* work: HTTP/1.1+SSE
framing, HTTP/2 routing, accumulating a response's stream events, and pairing a response
with its request by time. It defers every provider-*specific* decision to a ``TurnBuilder``:

  * which HTTP request is a model request (``is_request``),
  * how a captured chunk parses into stream events (``parse_events``),
  * when the accumulated events end a turn (``is_terminal``),
  * how events / a full response body assemble into the turn dict (``build_from_events`` /
    ``build_from_message``) — text, usage, model, and the paired request summary.

``pyagy`` supplies a ``GenaiTurnBuilder`` (cloudcode/Gemini shape); ``pycodex`` supplies a
``ResponsesTurnBuilder`` (OpenAI Responses shape). Both stay stdlib-pure.
"""


class TurnBuilder:
    kind = "turn"

    def is_request(self, msg):
        """Is this decoded HTTP/1.1 ``Message`` a model request worth tracking? Default: any."""
        return True

    def parse_events(self, data):
        """Parse a captured response chunk (bytes) into a list of stream-event dicts."""
        raise NotImplementedError

    def is_terminal(self, events):
        """Have the accumulated stream events reached the end of a turn?"""
        raise NotImplementedError

    def build_from_events(self, events, resp_t, resp_stream, req):
        """Assemble the turn dict from accumulated stream events + an optional paired request
        ``(req_t, req_stream, req_repr)`` (``None`` if unpaired)."""
        raise NotImplementedError

    def build_from_message(self, req, resp_t, resp_stream, msg):
        """Assemble the turn dict from a full HTTP response ``Message`` (offline / wire path)."""
        raise NotImplementedError
