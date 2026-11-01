"""OpenAI Responses-API turn shaping — the codex analog of pyagy's genai decode.

codex talks to ``/v1/responses`` with ``stream: true`` and consumes an SSE (or WebSocket)
stream of ``ResponsesStreamEvent`` JSON objects, each an event with a ``type`` and a few
optional fields (see codex-rs/codex-api/src/sse/responses.rs). The patched codex hands the
embedded worker one such event per ``codex_event`` fire (byte-identical on both transports),
plus the serialized request as ``codex_request``. This module assembles those into a
``codex_turn`` dict — the codex sibling of ``genai_turn`` — and adapts them to the shared
``wirecap.decode.turns.TurnBuilder`` interface the correlator drives.

Stdlib-only (loaded by codex's embedded interpreter).
"""
from collections.abc import Sequence

from wirecap.types import Event, EventSink, JSONObject, JSONValue, StreamId, json_array, json_loads, json_object

from wirecap.decode.turns import CapturedRequest, TurnBuilder

# Terminal event kinds: a turn ends when the server reports completion (or a failure/incomplete
# stream). `response.completed` carries the usage + final response object.
_TERMINAL = ("response.completed", "response.failed", "response.incomplete")


def assemble_text(events: Sequence[JSONObject]) -> str:
    """Read streamed text, with completed message items as a non-streaming fallback."""
    out: list[str] = []
    for e in events:
        delta = e.get("delta")
        if e.get("type") == "response.output_text.delta" and isinstance(delta, str):
            out.append(delta)
    if out:
        return "".join(out)
    for event in events:
        item = event.get("item")
        if (event.get("type") == "response.output_item.done" and isinstance(item, dict)
                and item.get("type") == "message" and item.get("role") == "assistant"):
            out.append(_item_text(item.get("content")))
    if out:
        return "".join(out)
    response = _completed_response(events)
    output = response.get("output") if response else None
    if isinstance(output, list):
        out.extend(_item_text(item.get("content")) for item in output
                   if isinstance(item, dict) and item.get("type") == "message"
                   and item.get("role") == "assistant")
    return "".join(out)


def assemble_reasoning(events: Sequence[JSONObject]) -> str:
    """Concatenate the reasoning trace (``response.reasoning_text.delta`` +
    ``response.reasoning_summary_text.delta``)."""
    out: list[str] = []
    for e in events:
        delta = e.get("delta")
        if e.get("type") in ("response.reasoning_text.delta",
                              "response.reasoning_summary_text.delta") and isinstance(delta, str):
            out.append(delta)
    return "".join(out)


def _completed_response(events: Sequence[JSONObject]) -> JSONObject | None:
    """The ``response`` object from the terminal ``response.completed`` event (dict or None)."""
    for e in reversed(events):
        response = e.get("response")
        if e.get("type") == "response.completed" and isinstance(response, dict):
            return response
    return None


def extract_usage(events: Sequence[JSONObject]) -> JSONObject:
    """Token usage from the completed event's ``response.usage`` (normalized to flat counts)."""
    resp = _completed_response(events)
    u = json_object((resp or {}).get("usage") or {})
    if not u:
        return {}
    return {
        "input_tokens": u.get("input_tokens"),
        "cached_input_tokens": json_object(u.get("input_tokens_details") or {}).get("cached_tokens"),
        "output_tokens": u.get("output_tokens"),
        "reasoning_output_tokens": json_object(u.get("output_tokens_details") or {}).get("reasoning_tokens"),
        "total_tokens": u.get("total_tokens"),
    }


def _headers_model(value: JSONValue) -> str | None:
    """openai-model / x-openai-model from a headers object (value may be a string or [string])."""
    if not isinstance(value, dict):
        return None
    for name, v in value.items():
        if name.lower() in ("openai-model", "x-openai-model"):
            if isinstance(v, str):
                return v
            if isinstance(v, list) and v and isinstance(v[0], str):
                return v[0]
    return None


def model_version(events: Sequence[JSONObject]) -> str | None:
    """The served model id. Prefer the completed response's ``model``; else the served
    ``openai-model`` header on any event's ``response.headers`` / top-level ``headers``."""
    resp = _completed_response(events)
    model = resp.get("model") if resp else None
    if isinstance(model, str) and model:
        return model
    for e in reversed(events):
        m = _headers_model(json_object(e.get("response") or {}).get("headers")) or _headers_model(e.get("headers"))
        if m:
            return m
    return None


def finish_reason(events: Sequence[JSONObject]) -> str | None:
    """The turn's terminal signal: ``end_turn`` on completed, else the terminal event kind."""
    for e in reversed(events):
        k = e.get("type")
        if k == "response.completed":
            resp = json_object(e.get("response") or {})
            return "end_turn" if resp.get("end_turn") else "completed"
        if isinstance(k, str) and k in ("response.failed", "response.incomplete"):
            return k.split(".", 1)[1]
    return None


def _item_text(content: JSONValue) -> str:
    """The concatenated text of a Responses input item's ``content`` (a string, or a list of
    ``{type: input_text|text, text}`` parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(text for p in content
                       if isinstance(p, dict) and isinstance(text := p.get("text"), str))
    return ""


def summarize_request(req_json: JSONObject) -> JSONObject:
    """Compact summary of a ``ResponsesApiRequest`` body (already json.loads'd): model,
    instructions length, tool names, and the user prompt. codex's ``input`` interleaves injected
    developer/context messages with the user turn, so the user prompt is the LAST ``role:"user"``
    message (after any injected context)."""
    instr = req_json.get("instructions") or ""
    if not isinstance(instr, str):
        raise TypeError("Responses instructions must be a string")
    tools = [t.get("name") for t in json_array(req_json.get("tools") or [])
             if isinstance(t, dict) and t.get("name")]
    user_text = ""
    inputs = json_array(req_json.get("input") or [])
    for item in inputs:
        if isinstance(item, dict) and item.get("role") == "user":
            txt = _item_text(item.get("content"))
            if txt:
                user_text = txt          # keep the last user message = the actual prompt
    return {
        "model": req_json.get("model"),
        "instructions_len": len(instr),
        "tools": tools,
        "num_input": len(inputs),
        "first_user_text": user_text,
    }


def build_turn_from_events(events: Sequence[JSONObject], resp_t: float | None,
                           resp_stream: StreamId, req: CapturedRequest | None) -> Event:
    """Assemble a ``codex_turn`` dict from accumulated Responses stream events + an optional
    request ``(req_t, req_stream, req_json)`` (``None`` if unpaired)."""
    turn: Event = {
        "kind": "codex_turn",
        "t": resp_t,
        "resp_stream": resp_stream,
        "text": assemble_text(events),
        "reasoning": assemble_reasoning(events),
        "model": model_version(events),
        "usage": extract_usage(events),
        "finish_reason": finish_reason(events),
        "n_events": len(events),
        "events": events,
    }
    if req is not None:
        qt, qsid, req_json = req
        turn["req_stream"] = qsid
        turn["req_t"] = qt
        if isinstance(req_json, dict):
            turn["request"] = summarize_request(req_json)
            turn["request_full"] = req_json
        else:
            turn["request"] = None
    return turn


class ResponsesTurnBuilder(TurnBuilder):
    """Adapt the Responses decode above to the shared ``TurnBuilder`` interface. codex hands us
    pre-parsed JSON (one event per ``codex_event`` fire), so only the pre-parsed correlator path
    (``feed_events`` / ``feed_request``) is used — the HTTP-framing methods (``is_request`` /
    ``build_from_message``) never fire."""


    def parse_events(self, data: bytes) -> list[JSONObject]:
        """One ``codex_event`` fire = one ResponsesStreamEvent JSON object."""
        try:
            obj = json_loads(data)
        except (ValueError, TypeError):
            return []
        return [obj] if isinstance(obj, dict) else []

    def is_terminal(self, events: Sequence[JSONObject]) -> bool:
        return any(e.get("type") in _TERMINAL for e in events)

    def build_from_events(self, events: Sequence[JSONObject], resp_t: float | None,
                          resp_stream: StreamId, req: CapturedRequest | None) -> Event:
        return build_turn_from_events(events, resp_t, resp_stream, req)


class ResponsesCorrelator:
    """Pair concurrent responses with the transport-owned native request ID."""

    def __init__(self, recorder: EventSink) -> None:
        self._recorder = recorder
        self._builder = ResponsesTurnBuilder()
        self._requests: dict[int, CapturedRequest] = {}
        self._events: dict[int, tuple[float, list[JSONObject]]] = {}

    def request(self, stream_id: int, request: JSONObject | None, t: float) -> None:
        self.finish(stream_id)
        self._requests[stream_id] = (t, stream_id, request)

    def feed_chunk(self, stream_id: int, data: bytes, t: float) -> None:
        events = self._builder.parse_events(data)
        if not events:
            return
        _, accumulated = self._events.setdefault(stream_id, (t, []))
        accumulated.extend(events)
        if self._builder.is_terminal(events):
            self.finish(stream_id)

    def finish(self, stream_id: int) -> None:
        request = self._requests.pop(stream_id, None)
        response = self._events.pop(stream_id, None)
        if response is not None:
            t, events = response
            self._recorder.event(build_turn_from_events(events, t, stream_id, request))
