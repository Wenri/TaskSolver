"""Decode kimi-code's wiretap events into ``kimi_turn`` dicts.

The vendored patch emits, per model request, one ``kimi_request`` (the agent-core-v2
``LLMRequestLogInput``: protocol/provider/model + the full normalized systemPrompt, tools and
messages) and a stream of ``kimi_event``s (``ModelRequestEvent``: ``part`` / ``usage`` /
``finish`` / ``timing`` — see the vendored
``packages/agent-core-v2/src/kosong/model/modelRequester.ts``). The ``finish`` event carries the
complete assistant ``Message``, so the assembled turn's text/tool-calls come from it, with the
streamed ``part`` deltas only as the fallback for a stream that dies before finishing.

Stdlib-only (the purity probe enforces it): this runs inside the embedded interpreter.
"""
from collections.abc import Sequence

from wirecap.types import Event, JSONObject, JSONValue, StreamId, json_array, json_loads, json_object

from wirecap.decode.turns import CapturedRequest, TurnBuilder


def _count(value: JSONValue) -> int:
    if value is None:
        return 0
    if not isinstance(value, int):
        raise TypeError("token count must be an integer")
    return value


def _text(value: JSONValue) -> str:
    return value if isinstance(value, str) else ""


def _content_text(parts: JSONValue, key: str) -> str:
    """Concatenated ``key`` fields of typed content parts (``{type, text|think}``)."""
    out: list[str] = []
    for p in json_array(parts or []):
        if isinstance(p, dict) and p.get("type") == key and isinstance(value := p.get(key), str):
            out.append(value)
    return "".join(out)


def _finish_event(events: Sequence[JSONObject]) -> JSONObject | None:
    for e in reversed(events):
        if e.get("type") == "finish":
            return e
    return None


def _streamed_text(events: Sequence[JSONObject], key: str) -> str:
    """Fallback assembly from the ``part`` deltas when no finish message arrived."""
    out: list[str] = []
    for e in events:
        if e.get("type") != "part":
            continue
        p = e.get("part")
        if isinstance(p, dict) and p.get("type") == key and isinstance(value := p.get(key), str):
            out.append(value)
    return "".join(out)


def extract_usage(events: Sequence[JSONObject]) -> JSONObject | None:
    """Normalize kosong's ``TokenUsage`` (inputOther/output/inputCacheRead/inputCacheCreation)
    to the neutral :class:`wirecap.decode.turns.Usage` field names."""
    for e in reversed(events):
        u = e.get("usage")
        if e.get("type") == "usage" and isinstance(u, dict):
            other = _count(u.get("inputOther"))
            cached = _count(u.get("inputCacheRead"))
            created = _count(u.get("inputCacheCreation"))
            output = _count(u.get("output"))
            return {
                "input_tokens": other + cached + created,
                "cached_input_tokens": cached,
                "output_tokens": output,
                "reasoning_output_tokens": 0,
                "total_tokens": other + cached + created + output,
                "raw": u,
            }
    return None


def model_version(events: Sequence[JSONObject]) -> str | None:
    for e in reversed(events):
        model = e.get("model")
        if e.get("type") == "usage" and isinstance(model, str) and model:
            return model
    return None


def _tool_calls(message: JSONObject | None) -> list[JSONObject]:
    calls: list[JSONObject] = []
    for c in json_array((message or {}).get("toolCalls") or []):
        if isinstance(c, dict):
            calls.append({"id": c.get("id"), "name": c.get("name"),
                          "arguments": c.get("arguments")})
    return calls


def _is_system_reminder(text: str) -> bool:
    """kimi-code 2.4+ appends harness context (date, permission mode) after the prompt as extra
    user messages, each one a ``<system-reminder>`` block."""
    return text.lstrip().startswith("<system-reminder>")


def _last_user_text(messages: JSONValue) -> str:
    text = ""
    for m in json_array(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            t = _content_text(m.get("content"), "text")
            if t and not _is_system_reminder(t):
                text = t                 # keep the last user message = the actual prompt
    return text


def summarize_request(req_json: JSONObject) -> JSONObject:
    """Compact summary of a ``kimi_request`` (``LLMRequestLogInput``) payload."""
    tools = [t.get("name") for t in json_array(req_json.get("tools") or [])
             if isinstance(t, dict) and t.get("name")]
    return {
        "model": req_json.get("modelName"),
        "model_alias": req_json.get("modelAlias"),
        "provider": req_json.get("providerType"),
        "protocol": req_json.get("protocol"),
        "thinking_effort": req_json.get("thinkingEffort"),
        "max_tokens": req_json.get("maxTokens"),
        "instructions_len": len(_text(req_json.get("systemPrompt"))),
        "tools": tools,
        "num_input": len(json_array(req_json.get("messages") or [])),
        "first_user_text": _last_user_text(req_json.get("messages")),
    }


def build_turn_from_events(events: Sequence[JSONObject], resp_t: float | None,
                           resp_stream: StreamId, req: CapturedRequest | None) -> Event:
    """Assemble a ``kimi_turn`` dict from accumulated ``ModelRequestEvent``s + an optional
    request ``(req_t, req_stream, req_json)`` (``None`` if unpaired)."""
    finish = _finish_event(events)
    message = json_object((finish or {}).get("message") or {})
    text = _content_text((message or {}).get("content"), "text") or _streamed_text(events, "text")
    reasoning = (_content_text((message or {}).get("content"), "think")
                 or _streamed_text(events, "think"))
    turn: Event = {
        "kind": "kimi_turn",
        "t": resp_t,
        "resp_stream": resp_stream,
        "text": text,
        "reasoning": reasoning,
        "tool_calls": _tool_calls(message),
        "model": model_version(events),
        "usage": extract_usage(events),
        "finish_reason": ((finish or {}).get("providerFinishReason")
                          or (finish or {}).get("rawFinishReason")),
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
                turn["model"] = req_json.get("modelName")
        else:
            turn["request"] = None
    return turn


class KimiTurnBuilder(TurnBuilder):
    """Adapt the ModelRequestEvent decode above to the shared ``TurnBuilder`` interface. The
    wiretap patch hands us pre-parsed JSON (one event per ``kimi_event`` fire), so only the
    pre-parsed correlator path (``feed_events`` / ``feed_request``) is used — the HTTP-framing
    methods are never called."""

    def parse_events(self, data: bytes) -> list[JSONObject]:
        """One ``kimi_event`` fire = one ModelRequestEvent JSON object."""
        try:
            obj = json_loads(data)
        except (ValueError, TypeError):
            return []
        return [obj] if isinstance(obj, dict) else []

    def is_terminal(self, events: Sequence[JSONObject]) -> bool:
        return any(e.get("type") == "finish" for e in events)

    def build_from_events(self, events: Sequence[JSONObject], resp_t: float | None,
                          resp_stream: StreamId, req: CapturedRequest | None) -> Event:
        return build_turn_from_events(events, resp_t, resp_stream, req)
