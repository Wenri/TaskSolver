"""Decode Claude Code's Anthropic Messages API traffic into ``claude_turn`` dicts.

Claude Code's bridged runtime taps parsed ``/v1/messages`` responses in
``claude/native_bridge.mjs`` and forwards each turn to the embedded worker
(``wirecap_node.node``) as:

  * ``claude_request`` — the serialized ``/v1/messages`` request JSON (one per turn).
  * ``claude_event``   — one parsed SSE event JSON (``message_start``,
    ``content_block_start``, ``content_block_delta``, ``content_block_stop``,
    ``message_delta``, ``message_stop``) or a single ``message`` object for a
    non-streaming call.
  * ``claude_end``     — stream iterator finished or closed early.

Stdlib-only (the purity probe enforces it): this runs inside the embedded interpreter.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from wirecap.decode.turns import CapturedRequest, TurnBuilder
from wirecap.types import Event, EventSink, JSONObject, JSONValue, StreamId, json_array, json_loads, json_object

_TERMINAL = ("message_stop", "message", "error")
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>")


def _count(value: JSONValue) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("token count must be an integer")
    return value


def _content_text(content: JSONValue, block_type: str = "text", field: str = "text") -> str:
    """Concatenate text from a string or a list of typed Anthropic content blocks."""
    if isinstance(content, str):
        return content if block_type == "text" else ""
    out: list[str] = []
    for item in json_array(content or []):
        if isinstance(item, dict) and item.get("type") == block_type:
            value = item.get(field)
            if isinstance(value, str):
                out.append(value)
    return "".join(out)


def _clean_user_text(text: str) -> str:
    cleaned = _SYSTEM_REMINDER_RE.sub("", text).strip()
    if cleaned.lstrip().startswith("<system-reminder>"):
        return ""
    return cleaned


def _user_message_text(content: JSONValue) -> str:
    if isinstance(content, str):
        return _clean_user_text(content)
    parts: list[str] = []
    for item in json_array(content or []):
        if isinstance(item, dict) and item.get("type") == "text":
            value = item.get("text")
            if isinstance(value, str):
                cleaned = _clean_user_text(value)
                if cleaned:
                    parts.append(cleaned)
    return "\n".join(parts)


def assemble_text(events: Sequence[JSONObject]) -> str:
    """Read streamed ``text_delta`` chunks, with non-streaming ``message`` blocks as fallback."""
    out: list[str] = []
    for event in events:
        kind = event.get("type")
        if kind == "content_block_start":
            block = json_object(event.get("content_block") or {})
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str) and text:
                out.append(text)
        elif kind == "content_block_delta":
            delta = json_object(event.get("delta") or {})
            text = delta.get("text")
            if delta.get("type") == "text_delta" and isinstance(text, str):
                out.append(text)
    if out:
        return "".join(out)
    for event in events:
        if event.get("type") == "message":
            text = _content_text(event.get("content"), "text", "text")
            if text:
                out.append(text)
    return "".join(out)


def assemble_reasoning(events: Sequence[JSONObject]) -> str:
    """Read streamed ``thinking_delta`` chunks, with non-streaming ``thinking`` blocks as fallback."""
    out: list[str] = []
    for event in events:
        kind = event.get("type")
        if kind == "content_block_start":
            block = json_object(event.get("content_block") or {})
            thinking = block.get("thinking")
            if block.get("type") == "thinking" and isinstance(thinking, str) and thinking:
                out.append(thinking)
        elif kind == "content_block_delta":
            delta = json_object(event.get("delta") or {})
            thinking = delta.get("thinking")
            if delta.get("type") == "thinking_delta" and isinstance(thinking, str):
                out.append(thinking)
    if out:
        return "".join(out)
    for event in events:
        if event.get("type") == "message":
            thinking = _content_text(event.get("content"), "thinking", "thinking")
            if thinking:
                out.append(thinking)
    return "".join(out)


def extract_tool_calls(events: Sequence[JSONObject]) -> list[JSONObject]:
    """Assemble ``tool_use`` blocks from streamed ``input_json_delta`` chunks or a ``message``."""
    by_index: dict[int, JSONObject] = {}
    partials: dict[int, list[str]] = {}
    order: list[int] = []
    for event in events:
        kind = event.get("type")
        index = event.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if kind == "content_block_start":
            block = json_object(event.get("content_block") or {})
            if block.get("type") == "tool_use":
                by_index[index] = {
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": block.get("input"),
                }
                partials[index] = []
                order.append(index)
        elif kind == "content_block_delta" and index in partials:
            delta = json_object(event.get("delta") or {})
            piece = delta.get("partial_json")
            if delta.get("type") == "input_json_delta" and isinstance(piece, str):
                partials[index].append(piece)
    if order:
        calls: list[JSONObject] = []
        for idx in order:
            call = dict(by_index[idx])
            joined = "".join(partials.get(idx, []))
            if joined:
                try:
                    call["arguments"] = json_loads(joined.encode("utf-8"))
                except (ValueError, TypeError):
                    call["arguments"] = joined
            calls.append(call)
        return calls
    fallback: list[JSONObject] = []
    for event in events:
        if event.get("type") == "message":
            for item in json_array(event.get("content") or []):
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    fallback.append({
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "arguments": item.get("input"),
                    })
    return fallback


def extract_usage(events: Sequence[JSONObject]) -> JSONObject | None:
    """Merge ``message_start`` and ``message_delta`` / ``message`` usage into normalized counts."""
    raw: JSONObject = {}
    for event in events:
        kind = event.get("type")
        usage: JSONValue = None
        if kind == "message_start":
            usage = json_object(event.get("message") or {}).get("usage")
        elif kind in ("message_delta", "message"):
            usage = event.get("usage")
        if isinstance(usage, dict):
            for key, value in usage.items():
                if value is not None:
                    raw[key] = value
    if not raw:
        return None
    uncached = _count(raw.get("input_tokens"))
    cached = _count(raw.get("cache_read_input_tokens"))
    created = _count(raw.get("cache_creation_input_tokens"))
    output = _count(raw.get("output_tokens"))
    input_total = uncached + cached + created
    return {
        "input_tokens": input_total,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": input_total + output,
        "raw": raw,
    }


def model_version(events: Sequence[JSONObject]) -> str | None:
    """The served model identifier from ``message_start`` or ``message``."""
    for event in reversed(events):
        kind = event.get("type")
        if kind == "message":
            model = event.get("model")
            if isinstance(model, str) and model:
                return model
        elif kind == "message_start":
            model = json_object(event.get("message") or {}).get("model")
            if isinstance(model, str) and model:
                return model
    return None


def finish_reason(events: Sequence[JSONObject]) -> str | None:
    """The stop reason from ``message_delta`` or ``message``."""
    for event in reversed(events):
        kind = event.get("type")
        if kind == "message_delta":
            reason = json_object(event.get("delta") or {}).get("stop_reason")
            if isinstance(reason, str) and reason:
                return reason
        elif kind == "message":
            reason = event.get("stop_reason")
            if isinstance(reason, str) and reason:
                return reason
        elif kind == "error":
            err = json_object(event.get("error") or {})
            err_type = err.get("type")
            return err_type if isinstance(err_type, str) and err_type else "error"
    if any(event.get("type") == "message_stop" for event in events):
        return "end_turn"
    return None


def summarize_request(req_json: JSONObject) -> JSONObject:
    """Compact summary of an Anthropic ``/v1/messages`` request payload."""
    system_text = _content_text(req_json.get("system"), "text", "text")
    tools = [t.get("name") for t in json_array(req_json.get("tools") or [])
             if isinstance(t, dict) and t.get("name")]
    messages = json_array(req_json.get("messages") or [])
    user_text = ""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            candidate = _user_message_text(message.get("content"))
            if candidate:
                user_text = candidate
    return {
        "model": req_json.get("model"),
        "max_tokens": req_json.get("max_tokens"),
        "instructions_len": len(system_text),
        "tools": tools,
        "num_input": len(messages),
        "first_user_text": user_text,
    }


def build_turn_from_events(events: Sequence[JSONObject], resp_t: float | None,
                           resp_stream: StreamId, req: CapturedRequest | None) -> Event:
    """Assemble a ``claude_turn`` dict from accumulated stream events + an optional request."""
    turn: Event = {
        "kind": "claude_turn",
        "t": resp_t,
        "resp_stream": resp_stream,
        "text": assemble_text(events),
        "reasoning": assemble_reasoning(events),
        "tool_calls": extract_tool_calls(events),
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
            if turn["model"] is None:
                turn["model"] = req_json.get("model")
        else:
            turn["request"] = None
    return turn


class ClaudeTurnBuilder(TurnBuilder):
    """Adapt the Anthropic Messages decode to the shared ``TurnBuilder`` interface."""

    def parse_events(self, data: bytes) -> list[JSONObject]:
        try:
            obj = json_loads(data)
        except (ValueError, TypeError):
            return []
        return [obj] if isinstance(obj, dict) else []

    def is_terminal(self, events: Sequence[JSONObject]) -> bool:
        return any(event.get("type") in _TERMINAL for event in events)

    def build_from_events(self, events: Sequence[JSONObject], resp_t: float | None,
                          resp_stream: StreamId, req: CapturedRequest | None) -> Event:
        return build_turn_from_events(events, resp_t, resp_stream, req)


class ClaudeCorrelator:
    """Pair concurrent ``/v1/messages`` responses with their native request ID."""

    def __init__(self, recorder: EventSink) -> None:
        self._recorder = recorder
        self._builder = ClaudeTurnBuilder()
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
