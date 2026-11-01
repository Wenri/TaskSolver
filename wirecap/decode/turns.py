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

Builders normalize token counts to :class:`Usage` fields and retain provider details under
``"raw"``, allowing shared primary-turn selection and usage totals.
"""
from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from typing import cast

from ..types import Event, JSONObject, StreamId
from .http1sse import Message

type CapturedRequest = tuple[float, StreamId, Message | JSONObject | None]


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
    raw: Mapping[str, object] = field(default_factory=dict[str, object])

    # These aliases remain part of pyagy's public API.
    @property
    def prompt_tokens(self) -> int:
        """Deprecated alias for :attr:`input_tokens`."""
        return self.input_tokens

    @property
    def candidates_tokens(self) -> int:
        """Deprecated alias for :attr:`output_tokens` (Gemini's name for completion tokens)."""
        return self.output_tokens


def _usage(turn: Mapping[str, object]) -> Mapping[str, object]:
    value = turn.get("usage")
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _count(usage: Mapping[str, object], field: str) -> int:
    value = usage.get(field)
    return value if isinstance(value, int) else 0


def primary_turn[TurnT: Mapping[str, object]](turns: Sequence[TurnT]) -> TurnT | None:
    """Return the turn with the most total tokens, or None for an empty input."""
    return max(turns, key=lambda turn: _count(_usage(turn), "total_tokens"), default=None)


def sum_usage(turns: Sequence[Mapping[str, object]]) -> Usage:
    """Sum normalized token counts and retain the primary turn's raw usage."""
    usage = Usage()
    for turn in turns:
        values = _usage(turn)
        usage.input_tokens += _count(values, "input_tokens")
        usage.cached_input_tokens += _count(values, "cached_input_tokens")
        usage.output_tokens += _count(values, "output_tokens")
        usage.reasoning_output_tokens += _count(values, "reasoning_output_tokens")
        usage.total_tokens += _count(values, "total_tokens")
    primary = primary_turn(turns)
    if primary:
        values = _usage(primary)
        raw = values.get("raw")
        usage.raw = cast(Mapping[str, object], raw) if isinstance(raw, Mapping) and raw else values
    return usage


class TurnBuilder:

    def is_request(self, msg: Message) -> bool:
        """Is this decoded HTTP/1.1 ``Message`` a model request worth tracking? Default: any."""
        return True

    def parse_events(self, data: bytes) -> list[JSONObject]:
        """Parse a captured response chunk (bytes) into a list of stream-event dicts."""
        raise NotImplementedError

    def is_terminal(self, events: Sequence[JSONObject]) -> bool:
        """Have the accumulated stream events reached the end of a turn?"""
        raise NotImplementedError

    def stream_key(self, event: JSONObject) -> str | None:
        """Which in-flight response ``event`` belongs to. ``None`` (the default) suits a CLI that
        streams one response at a time; a builder whose CLI overlaps responses returns an id
        carried by every event, so each response accumulates and ends on its own."""
        return None

    def request_matches(self, req_repr: Message | JSONObject | None,
                        events: Sequence[JSONObject]) -> bool | None:
        """Could the pending request ``req_repr`` have produced ``events``? ``None`` = no opinion;
        pairing then goes by time alone, as it does for every candidate without a verdict."""
        return None

    def build_from_events(self, events: Sequence[JSONObject], resp_t: float | None,
                          resp_stream: StreamId, req: CapturedRequest | None) -> Event:
        """Assemble the turn dict from accumulated stream events + an optional paired request
        ``(req_t, req_stream, req_repr)`` (``None`` if unpaired)."""
        raise NotImplementedError

    def build_from_message(self, req: CapturedRequest | None, resp_t: float | None,
                           resp_stream: StreamId, msg: Message) -> Event:
        """Assemble the turn dict from a full HTTP response ``Message`` (offline / wire path)."""
        raise NotImplementedError
