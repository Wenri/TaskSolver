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

Every builder's ``extract_usage`` normalizes provider token counts to the flat neutral names of
:class:`Usage` below (keeping the provider's own dict under ``"raw"``), so one Usage type,
one primary-turn picker and one summation serve both providers.
"""
from dataclasses import dataclass, field


@dataclass
class Usage:
    """Provider-neutral token accounting, summed across a response's turns. ``raw`` is the primary
    turn's untouched provider dict — Gemini's per-modality/tool-use breakdowns and OpenAI's
    ``*_details`` have no neutral equivalent and would otherwise be lost."""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    raw: dict = field(default_factory=dict)

    # Back-compat aliases: `pyagy`'s Usage predated the neutral names and these are a documented
    # public surface (test_scripts/test_client.py asserts on them).
    @property
    def prompt_tokens(self):
        """Deprecated alias for :attr:`input_tokens`."""
        return self.input_tokens

    @property
    def candidates_tokens(self):
        """Deprecated alias for :attr:`output_tokens` (Gemini's name for completion tokens)."""
        return self.output_tokens


_USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
                 "reasoning_output_tokens", "total_tokens")


def primary_turn(turns):
    """The substantive model turn (most total tokens). Both providers fire a small secondary call
    per run — agy's title generation, codex's per-exec side call — that the answer turn dwarfs.
    ``None`` if nothing decoded."""
    if not turns:
        return None
    return max(turns, key=lambda t: (t.get("usage") or {}).get("total_tokens") or 0)


def sum_usage(turns):
    """Sum :class:`Usage` across ``turns``, carrying the primary turn's provider-raw dict."""
    u = Usage()
    for t in turns:
        m = t.get("usage") or {}
        for f in _USAGE_FIELDS:
            setattr(u, f, getattr(u, f) + (m.get(f) or 0))
    p = primary_turn(turns)
    if p:
        u.raw = (p.get("usage") or {}).get("raw") or p.get("usage") or {}
    return u


class TurnBuilder:

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
