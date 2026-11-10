"""agy's genai (cloudcode/Gemini) turn shaping over the shared HTTP/1.1+SSE framing.

The provider-neutral framing (``Message`` / ``StreamDecoder`` / ``dechunk`` / ``inflate`` /
``parse_headers`` / ``parse_sse`` / ``classify``) lives in ``wirecap.decode.http1sse`` and is
re-exported here so this module keeps the full public surface. This file adds only the
*genai-specific* decode: agy talks to ``daily-cloudcode-pa.googleapis.com`` with
``POST /v1internal:streamGenerateContent?alt=sse`` and the reply is a ``text/event-stream``
whose ``data:`` events wrap the Gemini response under a ``"response"`` key. The functions
below assemble those into a ``genai_turn`` dict; ``GenaiTurnBuilder`` adapts them to the
shared ``TurnBuilder`` interface the correlator drives.

Stdlib-only (imported by agy's embedded interpreter).
"""
from collections.abc import Sequence
import re

from wirecap.types import (
    Event,
    JSONObject,
    JSONValue,
    StreamId,
    json_array,
    json_loads_object,
    json_object,
)

from wirecap.decode.http1sse import (   # re-exported framing (keeps this module's public surface)
    Message as Message, StreamDecoder as StreamDecoder, classify as classify, dechunk as dechunk,
    inflate as inflate, parse_headers as parse_headers, parse_sse as parse_sse,
)
from wirecap.decode.turns import CapturedRequest, TurnBuilder


def _candidates(event: JSONObject) -> list[JSONObject]:
    # cloudcode wraps the Gemini response under a "response" key; older/direct
    # generateContent puts candidates at the top level. Handle both.
    r = json_object(event.get("response", event))
    return [json_object(value) for value in json_array(r.get("candidates") or [])]


def assemble_text(events: Sequence[JSONObject], thoughts: bool = False) -> str:
    """Concatenate ``candidates[].content.parts[].text`` across streamed events: the answer
    parts, or with ``thoughts=True`` the reasoning parts (``"thought": true``). Thought parts
    used to be joined into the answer, so a turn that reasoned before answering returned its
    reasoning as the reply."""
    out: list[str] = []
    for e in events:
        for cand in _candidates(e):
            content = json_object(cand.get("content") or {})
            for value in json_array(content.get("parts") or []):
                part = json_object(value)
                text = part.get("text")
                if isinstance(text, str) and bool(part.get("thought")) == thoughts:
                    out.append(text)
    return "".join(out)


def extract_tool_calls(events: Sequence[JSONObject]) -> list[JSONObject]:
    """Extract ``candidates[].content.parts[].functionCall`` across streamed events."""
    calls: list[JSONObject] = []
    for e in events:
        for cand in _candidates(e):
            content = json_object(cand.get("content") or {})
            for value in json_array(content.get("parts") or []):
                fc = json_object(json_object(value).get("functionCall") or {})
                if fc:
                    calls.append({
                        "id": fc.get("id"),
                        "name": fc.get("name"),
                        "arguments": fc.get("args"),
                    })
    return calls


def extract_usage(events: Sequence[JSONObject]) -> JSONObject:
    """Token usage from the last ``usageMetadata`` seen across the stream, NORMALIZED to the same
    flat, provider-neutral counts the codex decoder emits (input/cached_input/output/
    reasoning_output/total) so one Usage type and one primary-turn picker serve both providers.

    Gemini's raw dict is preserved under ``"raw"``: it carries fields with no neutral equivalent
    (per-modality breakdowns, tool-use counts), and dropping them would lose data the capture
    already had."""
    for e in reversed(events):
        u = json_object(e.get("response", e)).get("usageMetadata")
        if isinstance(u, dict) and u:
            return {
                "input_tokens": u.get("promptTokenCount"),
                "cached_input_tokens": u.get("cachedContentTokenCount"),
                "output_tokens": u.get("candidatesTokenCount"),
                "reasoning_output_tokens": u.get("thoughtsTokenCount"),
                "total_tokens": u.get("totalTokenCount"),
                "raw": u,
            }
    return {}


def finish_reason(events: Sequence[JSONObject]) -> str | None:
    for e in reversed(events):
        for cand in _candidates(e):
            fr = cand.get("finishReason")
            if isinstance(fr, str) and fr:
                return fr
    return None


def model_version(events: Sequence[JSONObject]) -> str | None:
    """The served model id from the response (Gemini's ``modelVersion``, e.g.
    ``gemini-3.1-flash-lite``). Present in every event; take the last seen."""
    for e in reversed(events):
        mv = json_object(e.get("response", e)).get("modelVersion")
        if isinstance(mv, str) and mv:
            return mv
    return None


_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
_SYSTEM_MESSAGE_RE = re.compile(r"<SYSTEM_MESSAGE>.*?</SYSTEM_MESSAGE>", re.S)
_SUMMARY_REQUESTS_RE = re.compile(r"# User Requests\n(.*?)(?:\n# |\Z)", re.S)


def _extract_user_text(contents: Sequence[JSONValue]) -> str:
    """Extract the latest user prompt from ``request.contents``."""
    user_requests: list[str] = []
    summary_requests: list[str] = []
    user_texts: list[str] = []
    any_texts: list[str] = []
    for ct in contents:
        obj = json_object(ct)
        role = obj.get("role")
        for p in json_array(obj.get("parts") or []):
            text = json_object(p).get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            any_texts.append(text)
            for match in _USER_REQUEST_RE.finditer(text):
                inner = match.group(1).strip()
                if inner:
                    user_requests.append(inner)
            if "<CONTEXT_SUMMARY>" in text:
                sec = _SUMMARY_REQUESTS_RE.search(text)
                if sec:
                    items = re.split(r"(?:^|\n)\d+\.\s+", sec.group(1))
                    if len(items) > 1 and items[-1].strip():
                        summary_requests.append(items[-1].strip())
            if role in (None, "user"):
                cleaned = _SYSTEM_MESSAGE_RE.sub("", text).strip()
                if cleaned and not cleaned.startswith(
                    ("The following is a <SYSTEM_MESSAGE>", "<CONTEXT_SUMMARY>")
                ):
                    user_texts.append(cleaned)
    if user_requests:
        return user_requests[-1]
    if user_texts:
        return user_texts[-1]
    if summary_requests:
        return summary_requests[-1]
    return any_texts[0] if any_texts else ""


def summarize_request(req_json: JSONObject) -> JSONObject:
    """Compact summary of a streamGenerateContent request body (already json.loads'd)."""
    req = json_object(req_json.get("request") or {})
    instructions = json_object(req.get("systemInstruction") or {})
    si = "".join(text for p in json_array(instructions.get("parts") or [])
                 if isinstance(text := json_object(p).get("text"), str))
    tools = [json_object(fd).get("name") for t in json_array(req.get("tools") or [])
             for fd in json_array(json_object(t).get("functionDeclarations") or [])]
    contents = json_array(req.get("contents") or [])
    user_text = _extract_user_text(contents)
    return {
        "model": req_json.get("model"),
        "requestType": req_json.get("requestType"),
        "requestId": req_json.get("requestId"),
        "project": req_json.get("project"),
        "system_instruction_len": len(si),
        "tools": tools,
        "generation_config": req.get("generationConfig"),
        "num_contents": len(contents),
        "first_user_text": user_text,
    }


def is_generate_content(start_line: str) -> bool:
    return "streamGenerateContent" in start_line or "generateContent" in start_line


def build_turn(req: CapturedRequest | None, resp: tuple[float | None, StreamId, Message]) -> Event:
    """Assemble a ``genai_turn`` dict from a (request, response) message pair.
    ``req`` may be ``None`` if no matching request was captured."""
    return build_turn_from_events(parse_sse(resp[2].body), resp[0], resp[1], req)


def build_turn_from_events(events: Sequence[JSONObject], resp_t: float | None,
                           resp_stream: StreamId, req: CapturedRequest | None) -> Event:
    """Assemble a ``genai_turn`` dict from already-parsed SSE events plus an optional
    request ``(req_t, req_stream, req_msg)``. The live correlator uses this directly:
    HTTP/1.1 SSE is pull-based, so the transport read has no entry-arg the trampoline can
    capture — instead agy's ``toStreamResponseChunk`` hands us each decoded ``data:`` line,
    which we parse into events as they stream. ``req`` may be ``None`` if unpaired."""
    turn: Event = {
        "kind": "genai_turn",
        "t": resp_t,
        "resp_stream": resp_stream,
        "text": assemble_text(events),
        "reasoning": assemble_text(events, thoughts=True),
        "tool_calls": extract_tool_calls(events),
        "model": model_version(events),   # served model id, straight from the response
        "usage": extract_usage(events),
        "finish_reason": finish_reason(events),
        "n_events": len(events),
        "events": events,
    }
    if req is not None:
        qt, qsid, msg = req
        if not isinstance(msg, Message):
            raise TypeError("genai request must be a captured HTTP message")
        turn["req_stream"] = qsid
        turn["req_t"] = qt
        turn["host"] = msg.headers.get("host")
        try:
            req_json = json_loads_object(msg.body)
            turn["request"] = summarize_request(req_json)
            turn["request_full"] = req_json
        except ValueError:
            turn["request"] = None
    return turn


class GenaiTurnBuilder(TurnBuilder):
    """Adapt the genai decode above to the shared ``TurnBuilder`` interface the correlator
    drives — agy's ``toStreamResponseChunk`` chunks parse via ``parse_sse``, a turn ends at
    the first ``finishReason``, and turns assemble into the ``genai_turn`` dict."""


    def is_request(self, msg: Message) -> bool:
        return is_generate_content(msg.start_line)

    def parse_events(self, data: bytes) -> list[JSONObject]:
        return parse_sse(data)

    def is_terminal(self, events: Sequence[JSONObject]) -> bool:
        return finish_reason(events) is not None

    def stream_key(self, event: JSONObject) -> str | None:
        # agy streams its session-title call alongside the answer turn; every event of one
        # streamGenerateContent response carries that response's responseId
        response_id = json_object(event.get("response", event)).get("responseId")
        return response_id if isinstance(response_id, str) else None

    def request_matches(self, req_repr: Message | JSONObject | None,
                        events: Sequence[JSONObject]) -> bool | None:
        """A request produced these events iff its model names the served ``modelVersion``
        (``models/gemini-3-pro`` → ``gemini-3-pro``; a tier suffix such as ``-low`` may differ)."""
        if not isinstance(req_repr, Message):
            return None
        served = model_version(events)
        try:
            requested = json_loads_object(req_repr.body).get("model")
        except (AttributeError, TypeError, ValueError):
            return None
        if not served or not isinstance(requested, str) or not requested:
            return None
        def model_base(name: str) -> str:
            name = name.removeprefix("models/")
            base, separator, tier = name.rpartition("-")
            return base if separator and tier in ("minimal", "low", "medium", "high") else name

        return model_base(served) == model_base(requested)

    def build_from_events(self, events: Sequence[JSONObject], resp_t: float | None,
                          resp_stream: StreamId, req: CapturedRequest | None) -> Event:
        return build_turn_from_events(events, resp_t, resp_stream, req)

    def build_from_message(self, req: CapturedRequest | None, resp_t: float | None,
                           resp_stream: StreamId, msg: Message) -> Event:
        return build_turn(req, (resp_t, resp_stream, msg))
