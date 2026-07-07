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
import json

from wirecap.decode.turns import TurnBuilder

# Terminal event kinds: a turn ends when the server reports completion (or a failure/incomplete
# stream). `response.completed` carries the usage + final response object.
_TERMINAL = ("response.completed", "response.failed", "response.incomplete")


def assemble_text(events):
    """Concatenate the assistant answer from ``response.output_text.delta`` events."""
    out = []
    for e in events:
        if e.get("type") == "response.output_text.delta" and e.get("delta"):
            out.append(e["delta"])
    return "".join(out)


def assemble_reasoning(events):
    """Concatenate the reasoning trace (``response.reasoning_text.delta`` +
    ``response.reasoning_summary_text.delta``)."""
    out = []
    for e in events:
        if e.get("type") in ("response.reasoning_text.delta",
                              "response.reasoning_summary_text.delta") and e.get("delta"):
            out.append(e["delta"])
    return "".join(out)


def _completed_response(events):
    """The ``response`` object from the terminal ``response.completed`` event (dict or None)."""
    for e in reversed(events):
        if e.get("type") == "response.completed" and isinstance(e.get("response"), dict):
            return e["response"]
    return None


def extract_usage(events):
    """Token usage from the completed event's ``response.usage`` (normalized to flat counts)."""
    resp = _completed_response(events)
    u = (resp or {}).get("usage") or {}
    if not u:
        return {}
    return {
        "input_tokens": u.get("input_tokens"),
        "cached_input_tokens": (u.get("input_tokens_details") or {}).get("cached_tokens"),
        "output_tokens": u.get("output_tokens"),
        "reasoning_output_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
        "total_tokens": u.get("total_tokens"),
    }


def _headers_model(value):
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


def model_version(events):
    """The served model id. Prefer the completed response's ``model``; else the served
    ``openai-model`` header on any event's ``response.headers`` / top-level ``headers``."""
    resp = _completed_response(events)
    if resp and resp.get("model"):
        return resp["model"]
    for e in reversed(events):
        m = _headers_model((e.get("response") or {}).get("headers")) or _headers_model(e.get("headers"))
        if m:
            return m
    return None


def finish_reason(events):
    """The turn's terminal signal: ``end_turn`` on completed, else the terminal event kind."""
    for e in reversed(events):
        k = e.get("type")
        if k == "response.completed":
            resp = e.get("response") or {}
            return "end_turn" if resp.get("end_turn") else "completed"
        if k in ("response.failed", "response.incomplete"):
            return k.split(".", 1)[1]
    return None


def _item_text(content):
    """The concatenated text of a Responses input item's ``content`` (a string, or a list of
    ``{type: input_text|text, text}`` parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def summarize_request(req_json):
    """Compact summary of a ``ResponsesApiRequest`` body (already json.loads'd): model,
    instructions length, tool names, and the user prompt. codex's ``input`` interleaves injected
    developer/context messages with the user turn, so the user prompt is the LAST ``role:"user"``
    message (after any injected context)."""
    instr = req_json.get("instructions") or ""
    tools = [t.get("name") for t in req_json.get("tools", []) or []
             if isinstance(t, dict) and t.get("name")]
    user_text = ""
    for item in req_json.get("input", []) or []:
        if isinstance(item, dict) and item.get("role") == "user":
            txt = _item_text(item.get("content"))
            if txt:
                user_text = txt          # keep the last user message = the actual prompt
    return {
        "model": req_json.get("model"),
        "instructions_len": len(instr),
        "tools": tools,
        "num_input": len(req_json.get("input", []) or []),
        "first_user_text": user_text,
    }


def build_turn_from_events(events, resp_t, resp_stream, req):
    """Assemble a ``codex_turn`` dict from accumulated Responses stream events + an optional
    request ``(req_t, req_stream, req_json)`` (``None`` if unpaired)."""
    turn = {
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
    (``feed_events`` / ``feed_request``) is used — the HTTP-framing hooks (``is_request`` /
    ``build_from_message``) never fire."""

    kind = "codex_turn"

    def parse_events(self, data):
        """One ``codex_event`` fire = one ResponsesStreamEvent JSON object."""
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            return []
        return [obj] if isinstance(obj, dict) else []

    def is_terminal(self, events):
        return any(e.get("type") in _TERMINAL for e in events)

    def build_from_events(self, events, resp_t, resp_stream, req):
        return build_turn_from_events(events, resp_t, resp_stream, req)
