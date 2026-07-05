"""Decode agy's model traffic — which is HTTP/1.1 + SSE, *not* HTTP/2.

agy talks to `daily-cloudcode-pa.googleapis.com` with
``POST /v1internal:streamGenerateContent?alt=sse HTTP/1.1`` and the reply is a
``text/event-stream`` of ``data: {...}`` lines. So the HTTP/2 reassembler
(`h2reassemble.py`) is the *wrong* decoder for this endpoint — this module is the
right one. (h2reassemble still applies to agy's other gRPC/HTTP-2 connections.)

Everything here is stdlib-only (brotli is imported lazily and optionally), so it is
safe to load inside agy's embedded system libpython. Two layers:

  * pure functions — ``dechunk`` / ``inflate`` / ``parse_sse`` / ``assemble_text`` /
    ``extract_usage`` / ``summarize_request`` — decode a *complete* body.
  * ``StreamDecoder`` — accumulates one direction's plaintext byte stream (fed
    incrementally by the tls hooks) and yields each complete HTTP/1.1 ``Message`` as
    soon as its framing closes (content-length reached, or the chunked 0-terminator).
"""
import json
import re
import zlib

# Request line OR response status line. `.search` skips any TLS-handshake plaintext
# prefix that may precede the first HTTP message on the read side.
_HDR = re.compile(
    rb"(POST|GET|PUT|PATCH|DELETE|HEAD|OPTIONS) ([^\r\n ]+) HTTP/1\.1\r\n"
    rb"|HTTP/1\.1 (\d{3})[^\r\n]*\r\n"
)
_METHODS = (b"POST ", b"GET ", b"PUT ", b"PATCH ", b"DELETE ", b"HEAD ", b"OPTIONS ")


class Message:
    """One decoded HTTP/1.1 message (request or response). ``body`` is de-chunked
    and content-decoded (gzip/br/deflate)."""

    __slots__ = ("start_line", "headers", "body")

    def __init__(self, start_line, headers, body):
        self.start_line = start_line
        self.headers = headers
        self.body = body

    @property
    def is_request(self):
        return not self.start_line.startswith("HTTP/1.1")

    @property
    def method(self):
        return self.start_line.split(" ", 1)[0] if self.is_request else None

    @property
    def path(self):
        parts = self.start_line.split(" ")
        return parts[1] if self.is_request and len(parts) > 1 else None

    @property
    def status(self):
        if self.is_request:
            return None
        parts = self.start_line.split(" ")
        try:
            return int(parts[1])
        except (IndexError, ValueError):
            return None

    @property
    def content_type(self):
        return self.headers.get("content-type", "")

    @property
    def is_event_stream(self):
        return "event-stream" in self.content_type or self.body[:6] == b"data: "

    def __repr__(self):
        return f"<Message {self.start_line!r} ctype={self.content_type!r} body={len(self.body)}B>"


def parse_headers(head_bytes):
    """Split a raw header block (start line + fields, no trailing CRLFCRLF) into
    ``(start_line, {lowercased_name: value})``."""
    lines = head_bytes.decode("latin1").split("\r\n")
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return lines[0], headers


def dechunk(buf, start):
    """De-chunk ``transfer-encoding: chunked`` starting at ``buf[start:]``.
    Returns ``(decoded_bytes, consumed)`` where ``consumed`` counts bytes from
    ``start`` through the 0-terminator, or ``(None, 0)`` if not yet complete."""
    out = bytearray()
    i = start
    n_buf = len(buf)
    while True:
        j = buf.find(b"\r\n", i)
        if j < 0:
            return None, 0
        try:
            size = int(bytes(buf[i:j]).split(b";")[0], 16)
        except ValueError:
            return None, 0
        i = j + 2
        if size == 0:
            # Final (empty-trailer) chunk: one more CRLF closes the message.
            end = buf.find(b"\r\n", i)
            if end < 0:
                return None, 0
            return bytes(out), (end + 2) - start
        if n_buf - i < size + 2:
            return None, 0
        out += buf[i:i + size]
        i += size + 2


def inflate(body, encoding):
    """Content-decode a body per ``content-encoding`` (gzip/br/deflate). Returns the
    body unchanged on any failure or unknown encoding."""
    try:
        if "gzip" in encoding:
            return zlib.decompress(body, wbits=zlib.MAX_WBITS | 16)
        if "br" in encoding:
            import brotli
            return brotli.decompress(body)
        if "deflate" in encoding:
            return zlib.decompress(body)
    except Exception:
        pass
    return body


def parse_sse(body):
    """Parse an SSE ``text/event-stream`` body into the list of JSON objects carried
    by its ``data:`` lines (``[DONE]`` sentinels skipped)."""
    events = []
    for chunk in re.split(rb"\r?\n\r?\n", body):
        for ln in chunk.split(b"\n"):
            ln = ln.strip()
            if ln.startswith(b"data:"):
                payload = ln[5:].strip()
                if payload and payload != b"[DONE]":
                    try:
                        events.append(json.loads(payload))
                    except ValueError:
                        pass
    return events


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


class StreamDecoder:
    """Accumulate one direction's plaintext byte stream and yield complete
    ``Message`` objects as their framing closes. Feed bytes in order via ``feed``;
    iterate the returned messages."""

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0            # scan start: bytes before here are consumed/discarded

    def feed(self, data):
        """Append ``data`` and return a list of newly-completed ``Message``s."""
        self.buf += data
        done = []
        while True:
            msg, end = self._try_one()
            if msg is None:
                break
            done.append(msg)
            self.pos = end
        # Reclaim consumed bytes so the buffer doesn't grow without bound.
        if self.pos > 1 << 20:
            del self.buf[:self.pos]
            self.pos = 0
        return done

    def _try_one(self):
        m = _HDR.search(self.buf, self.pos)
        if not m:
            return None, self.pos
        hs = m.start()
        he = self.buf.find(b"\r\n\r\n", hs)
        if he < 0:
            return None, self.pos          # headers not complete yet
        start_line, headers = parse_headers(bytes(self.buf[hs:he]))
        bstart = he + 4
        te = headers.get("transfer-encoding", "")
        if "chunked" in te:
            raw, consumed = dechunk(self.buf, bstart)
            if raw is None:
                return None, self.pos      # need more chunk data
            end = bstart + consumed
        elif "content-length" in headers:
            try:
                n = int(headers["content-length"])
            except ValueError:
                n = 0
            if len(self.buf) - bstart < n:
                return None, self.pos      # body not fully arrived
            raw = bytes(self.buf[bstart:bstart + n])
            end = bstart + n
        else:
            is_request = not start_line.startswith("HTTP/1.1")
            if is_request:
                raw, end = b"", bstart      # bodyless request (e.g. GET)
            else:
                # A response with neither length nor chunked isn't delimited until the
                # connection closes; wait unless a following message start is visible.
                nxt = _HDR.search(self.buf, bstart)
                if not nxt:
                    return None, self.pos
                raw = bytes(self.buf[bstart:nxt.start()])
                end = nxt.start()
        body = inflate(raw, headers.get("content-encoding", ""))
        return Message(start_line, headers, body), end


def is_generate_content(start_line):
    return "streamGenerateContent" in start_line or "generateContent" in start_line


def classify(direction, buf):
    """Sniff whether a plaintext stream is HTTP/1.1 (model endpoint) or HTTP/2
    (agy's gRPC connections). Returns ``"http1"``, ``"h2"``, or ``None`` (undecided —
    feed more bytes)."""
    if direction == "c2s":
        if buf[:14] == b"PRI * HTTP/2.0":
            return "h2"
        if buf[:14].startswith(b"PRI"):        # partial preface
            return None
        if any(buf.startswith(mth) for mth in _METHODS):
            return "http1"
        # not yet enough to tell a method apart
        if len(buf) >= 8:
            return "h2"                         # non-HTTP/1.1 opening → assume h2
        return None
    else:
        if buf[:9] == b"HTTP/1.1 ":
            return "http1"
        if buf[:5] == b"HTTP/":                 # partial status line
            return None
        if len(buf) >= 9 and buf[3] == 0x04:    # HTTP/2 server preface = SETTINGS
            return "h2"
        if len(buf) >= 9:
            return "h2"
        return None


def decode_capture(path):
    """Offline: reconstruct genai turns from a capture JSONL of raw tls events.
    Only works when the events carry full bodies (record with a large
    ``AGY_PROC_PREVIEW``); the live ``capture.Correlator`` doesn't have that limit.
    Returns a list of turn dicts (see ``capture.Correlator._emit_turn``)."""
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
    request. The live correlator uses this directly: HTTP/1.1 SSE is pull-based, so the
    transport read has no entry-arg the trampoline can capture — instead agy's
    ``toStreamResponseChunk`` hands us each decoded ``data:`` line, which we parse into
    events as they stream, rather than re-decoding a whole HTTP response body. ``req`` may
    be ``None`` if no matching request was captured."""
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
