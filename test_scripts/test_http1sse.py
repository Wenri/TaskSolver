#!/usr/bin/env python3
"""Offline unit tests for the HTTP/1.1 + SSE model-traffic decoder and the live
correlator. No agy, no network, no credentials — the fixture is a format-accurate
synthetic streamGenerateContent turn (a real capture would embed a bearer token, so
we never check one in). Exercises: request-body JSON parse, chunked de-framing across
split feeds, gzip inflate, the ``response``-wrapped SSE candidates, usage extraction,
stream classification, and the *Conn/*halfConn cross-stream correlation.

    python3 test_scripts/test_http1sse.py
"""
import gzip
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANTIGRAVITY = os.path.join(os.path.dirname(_HERE), "antigravity")
sys.path.insert(0, _ANTIGRAVITY)

from pyagy.agy_process import http1sse as h  # noqa: E402
from pyagy.agy_process import capture         # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def chunked(body: bytes) -> bytes:
    """Encode ``body`` as one HTTP/1.1 chunk + the 0-terminator."""
    return b"%X\r\n%s\r\n0\r\n\r\n" % (len(body), body)


# --- build the fixture -------------------------------------------------------
REQUEST_JSON = {
    "project": "projects/demo",
    "requestId": "req-abc-123",
    "model": "models/gemini-3-pro",
    "requestType": "AGENT",
    "request": {
        "systemInstruction": {"parts": [{"text": "You are a helpful agent."}]},
        "contents": [{"role": "user", "parts": [{"text": "Say ZORPLE"}]}],
        "tools": [{"functionDeclarations": [
            {"name": "read_file"}, {"name": "run_shell"}]}],
        "generationConfig": {"temperature": 0.0},
    },
}
_req_body = json.dumps(REQUEST_JSON).encode()
REQUEST_BYTES = (
    b"POST /v1internal:streamGenerateContent?alt=sse HTTP/1.1\r\n"
    b"Host: daily-cloudcode-pa.googleapis.com\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_req_body)).encode() + b"\r\n"
    b"\r\n" + _req_body
)

# SSE: candidates are wrapped under "response"; text arrives in two events, then a
# final event carries the finishReason + usageMetadata (last-seen usage wins).
_sse = b"".join([
    b'data: ' + json.dumps({"response": {"candidates": [
        {"content": {"parts": [{"text": "ZOR"}]}}]}}).encode() + b"\n\n",
    b'data: ' + json.dumps({"response": {"candidates": [
        {"content": {"parts": [{"text": "PLE"}]}}]}}).encode() + b"\n\n",
    b'data: ' + json.dumps({"response": {
        "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 1,
                          "totalTokenCount": 13}}}).encode() + b"\n\n",
    b"data: [DONE]\n\n",
])
RESPONSE_BYTES = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/event-stream\r\n"
    b"Transfer-Encoding: chunked\r\n"
    b"\r\n" + chunked(_sse)
)


def test_request_decode():
    print("[request] POST streamGenerateContent body")
    dec = h.StreamDecoder()
    msgs = dec.feed(REQUEST_BYTES)
    check(len(msgs) == 1, "request: exactly one message")
    m = msgs[0]
    check(m.is_request and m.method == "POST", "request: parsed as POST")
    check(h.is_generate_content(m.start_line), "request: recognized as generateContent")
    check(m.headers.get("host") == "daily-cloudcode-pa.googleapis.com", "request: Host header")
    body = json.loads(m.body)
    check(body["requestId"] == "req-abc-123", "request: body JSON round-trips")
    s = h.summarize_request(body)
    check(s["model"] == "models/gemini-3-pro", "request: model extracted")
    check(s["tools"] == ["read_file", "run_shell"], "request: tool names extracted")
    check(s["first_user_text"] == "Say ZORPLE", "request: first user text extracted")
    check(s["system_instruction_len"] == len("You are a helpful agent."),
          "request: systemInstruction length")


def test_response_decode_incremental():
    print("[response] chunked SSE, fed one byte at a time")
    dec = h.StreamDecoder()
    done = []
    for i in range(len(RESPONSE_BYTES)):       # byte-at-a-time stresses reassembly
        done += dec.feed(RESPONSE_BYTES[i:i + 1])
    check(len(done) == 1, "response: one message after full stream")
    m = done[0]
    check(m.status == 200 and m.is_event_stream, "response: 200 event-stream")
    events = h.parse_sse(m.body)
    check(len(events) == 3, "response: 3 data events (DONE skipped)")
    check(h.assemble_text(events) == "ZORPLE", "response: text assembles to ZORPLE")
    check(h.finish_reason(events) == "STOP", "response: finishReason STOP")
    check(h.extract_usage(events).get("totalTokenCount") == 13, "response: usage extracted")


def test_inflate_gzip():
    print("[inflate] gzip content-encoding")
    raw = gzip.compress(b'{"hello":"world"}')
    check(h.inflate(raw, "gzip") == b'{"hello":"world"}', "inflate: gzip round-trip")
    check(h.inflate(b"plain", "") == b"plain", "inflate: passthrough on no encoding")


def test_classify():
    print("[classify] http1 vs h2 sniffing")
    check(h.classify("c2s", b"POST /x HTTP/1.1\r\n") == "http1", "classify: c2s POST → http1")
    check(h.classify("c2s", b"PRI * HTTP/2.0\r\n\r\n") == "h2", "classify: c2s preface → h2")
    check(h.classify("s2c", b"HTTP/1.1 200 OK\r\n") == "http1", "classify: s2c status → http1")
    check(h.classify("c2s", b"PO") is None, "classify: too-few bytes undecided")


def test_correlator_cross_stream():
    print("[correlator] *Conn (c2s) vs *halfConn (s2c) — different stream ids")

    class FakeRec:
        def __init__(self):
            self.events = []

        def event(self, obj):
            self.events.append(obj)

    rec = FakeRec()
    corr = capture.Correlator(rec, reassembler=None)
    # request on stream 0xAAAA (a *Conn), response on 0xBBBB (a *halfConn)
    corr.feed("c2s", 0xAAAA, REQUEST_BYTES, t=100.0)
    corr.feed("s2c", 0xBBBB, RESPONSE_BYTES, t=100.5)
    turns = [e for e in rec.events if e.get("kind") == "genai_turn"]
    check(len(turns) == 1, "correlator: one genai_turn emitted")
    if turns:
        turn = turns[0]
        check(turn["text"] == "ZORPLE", "correlator: assembled text")
        check(turn["req_stream"] == 0xAAAA and turn["resp_stream"] == 0xBBBB,
              "correlator: paired across distinct stream ids")
        check(turn.get("request", {}).get("requestId") == "req-abc-123",
              "correlator: request summary attached")
        check(turn["usage"].get("totalTokenCount") == 13, "correlator: usage attached")


def test_correlator_resp_chunk():
    print("[correlator] live resp_chunk path — request off the wire, response via SSE lines")

    class FakeRec:
        def __init__(self):
            self.events = []

        def event(self, obj):
            self.events.append(obj)

    # The response side no longer arrives on s2c (that transport read is retired); agy's
    # toStreamResponseChunk hook hands us each decoded `data:` line one at a time.
    resp_lines = [
        b'data: ' + json.dumps({"response": {"modelVersion": "gemini-3-pro", "candidates": [
            {"content": {"parts": [{"text": "ZOR"}]}}]}}).encode(),
        b'data: ' + json.dumps({"response": {"modelVersion": "gemini-3-pro", "candidates": [
            {"content": {"parts": [{"text": "PLE"}]}}]}}).encode(),
        b'data: ' + json.dumps({"response": {"modelVersion": "gemini-3-pro",
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 1,
                              "totalTokenCount": 13}}}).encode(),
        b'data: [DONE]',
    ]

    rec = FakeRec()
    corr = capture.Correlator(rec, reassembler=None)
    corr.feed("c2s", 0xAAAA, REQUEST_BYTES, t=100.0)     # request off the wire (tls_write)
    for i, ln in enumerate(resp_lines):                  # response, one SSE line per fire
        corr.feed_resp_chunk(ln, t=100.5 + i * 0.01)
    turns = [e for e in rec.events if e.get("kind") == "genai_turn"]
    check(len(turns) == 1, "resp_chunk: exactly one genai_turn (emitted on finishReason)")
    if turns:
        turn = turns[0]
        check(turn["text"] == "ZORPLE", "resp_chunk: text assembles across lines")
        check(turn["finish_reason"] == "STOP", "resp_chunk: finishReason STOP")
        check(turn["model"] == "gemini-3-pro", "resp_chunk: served model decoded from response")
        check(turn["usage"].get("totalTokenCount") == 13, "resp_chunk: usage extracted")
        check(turn["req_stream"] == 0xAAAA, "resp_chunk: paired with the wire request")
        check(turn.get("request", {}).get("requestId") == "req-abc-123",
              "resp_chunk: request summary attached")
    # a trailing [DONE] after the finishReason emit must not start/emit a second turn
    check(len([e for e in rec.events if e.get("kind") == "genai_turn"]) == 1,
          "resp_chunk: [DONE] after finish does not emit a second turn")


def test_import_purity():
    print("[purity] agy_process imports under python3 -S with no tasksolver")
    code = ("import sys; sys.path.insert(0, %r); import pyagy.agy_process; "
            "assert 'tasksolver' not in sys.modules; print('pure')" % _ANTIGRAVITY)
    r = subprocess.run([sys.executable, "-S", "-c", code],
                       capture_output=True, text=True)
    check(r.returncode == 0 and "pure" in r.stdout,
          "purity: pyagy.agy_process is stdlib-only (python3 -S)")
    if r.returncode != 0:
        print(r.stderr)


def main():
    test_request_decode()
    test_response_decode_incremental()
    test_inflate_gzip()
    test_classify()
    test_correlator_cross_stream()
    test_correlator_resp_chunk()
    test_import_purity()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
