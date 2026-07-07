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
import json

from wirecap.decode.http1sse import (   # re-exported framing (keeps this module's public surface)
    Message, StreamDecoder, classify, dechunk, inflate, parse_headers, parse_sse,
)
from wirecap.decode.turns import TurnBuilder


def _candidates(event):
    # cloudcode wraps the Gemini response under a "response" key; older/direct
    # generateContent puts candidates at the top level. Handle both.
    r = event.get("response", event)
    return r.get("candidates", []) or []


def assemble_text(events):
    """Concatenate ``candidates[].content.parts[].text`` across streamed events."""
    out = []
    for e in events:
        for cand in _candidates(e):
            for part in (cand.get("content", {}) or {}).get("parts", []) or []:
                if "text" in part:
                    out.append(part["text"])
    return "".join(out)


def extract_usage(events):
    """The last ``usageMetadata`` seen (token counts) across the stream."""
    for e in reversed(events):
        u = e.get("response", e).get("usageMetadata")
        if u:
            return u
    return {}


def finish_reason(events):
    for e in reversed(events):
        for cand in _candidates(e):
            fr = cand.get("finishReason")
            if fr:
                return fr
    return None


def model_version(events):
    """The served model id from the response (Gemini's ``modelVersion``, e.g.
    ``gemini-3.1-flash-lite``). Present in every event; take the last seen."""
    for e in reversed(events):
        mv = e.get("response", e).get("modelVersion")
        if mv:
            return mv
    return None


def summarize_request(req_json):
    """Compact summary of a streamGenerateContent request body (already json.loads'd)."""
    req = req_json.get("request", {}) or {}
    si = "".join(p.get("text", "") for p in
                 (req.get("systemInstruction", {}) or {}).get("parts", []) or [])
    tools = [fd.get("name") for t in req.get("tools", []) or []
             for fd in t.get("functionDeclarations", []) or []]
    user_text = next((p["text"] for ct in req.get("contents", []) or []
                      for p in ct.get("parts", []) or [] if "text" in p), "")
    return {
        "model": req_json.get("model"),
        "requestType": req_json.get("requestType"),
        "requestId": req_json.get("requestId"),
        "project": req_json.get("project"),
        "system_instruction_len": len(si),
        "tools": tools,
        "generation_config": req.get("generationConfig"),
        "num_contents": len(req.get("contents", []) or []),
        "first_user_text": user_text,
    }


def is_generate_content(start_line):
    return "streamGenerateContent" in start_line or "generateContent" in start_line


def decode_capture(path):
    """Offline: reconstruct genai turns from a capture JSONL of raw tls events.
    Only works when the events carry full bodies (record with a large ``AGY_PROC_PREVIEW``);
    the live ``capture.Correlator`` doesn't have that limit. Returns a list of turn dicts."""
    import collections
    streams = collections.defaultdict(list)     # (dir, stream) -> [(t, bytes)]
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        k = r.get("kind")
        if k not in ("tls_write", "tls_read") or not r.get("head"):
            continue
        d = "c2s" if k == "tls_write" else "s2c"
        streams[(d, r["stream"])].append((r["t"], bytes.fromhex(r["head"])))

    requests, responses = [], []
    for (d, sid), chunks in streams.items():
        dec = StreamDecoder()
        chunks.sort(key=lambda x: x[0])
        for t, b in chunks:
            for msg in dec.feed(b):
                if d == "c2s" and msg.is_request and is_generate_content(msg.start_line):
                    requests.append((t, sid, msg))
                elif d == "s2c" and msg.is_event_stream:
                    responses.append((t, sid, msg))
    return _pair(requests, responses)


def _pair(requests, responses):
    """Correlate each response to the nearest preceding request (by time)."""
    requests.sort(key=lambda x: x[0])
    turns = []
    for rt, rsid, resp in sorted(responses, key=lambda x: x[0]):
        req = None
        for qt, qsid, m in requests:
            if qt <= rt + 1.0:
                req = (qt, qsid, m)
            else:
                break
        turns.append(build_turn(req, (rt, rsid, resp)))
    return turns


def build_turn(req, resp):
    """Assemble a ``genai_turn`` dict from a (request, response) message pair.
    ``req`` may be ``None`` if no matching request was captured."""
    return build_turn_from_events(parse_sse(resp[2].body), resp[0], resp[1], req)


def build_turn_from_events(events, resp_t, resp_stream, req):
    """Assemble a ``genai_turn`` dict from already-parsed SSE events plus an optional
    request ``(req_t, req_stream, req_msg)``. The live correlator uses this directly:
    HTTP/1.1 SSE is pull-based, so the transport read has no entry-arg the trampoline can
    capture — instead agy's ``toStreamResponseChunk`` hands us each decoded ``data:`` line,
    which we parse into events as they stream. ``req`` may be ``None`` if unpaired."""
    turn = {
        "kind": "genai_turn",
        "t": resp_t,
        "resp_stream": resp_stream,
        "text": assemble_text(events),
        "model": model_version(events),   # served model id, straight from the response
        "usage": extract_usage(events),
        "finish_reason": finish_reason(events),
        "n_events": len(events),
        "events": events,
    }
    if req is not None:
        qt, qsid, msg = req
        turn["req_stream"] = qsid
        turn["req_t"] = qt
        turn["host"] = msg.headers.get("host")
        try:
            req_json = json.loads(msg.body)
            turn["request"] = summarize_request(req_json)
            turn["request_full"] = req_json
        except ValueError:
            turn["request"] = None
    return turn


class GenaiTurnBuilder(TurnBuilder):
    """Adapt the genai decode above to the shared ``TurnBuilder`` interface the correlator
    drives — agy's ``toStreamResponseChunk`` chunks parse via ``parse_sse``, a turn ends at
    the first ``finishReason``, and turns assemble into the ``genai_turn`` dict."""

    kind = "genai_turn"

    def is_request(self, msg):
        return is_generate_content(msg.start_line)

    def parse_events(self, data):
        return parse_sse(data)

    def is_terminal(self, events):
        return finish_reason(events) is not None

    def build_from_events(self, events, resp_t, resp_stream, req):
        return build_turn_from_events(events, resp_t, resp_stream, req)

    def build_from_message(self, req, resp_t, resp_stream, msg):
        return build_turn(req, (resp_t, resp_stream, msg))
