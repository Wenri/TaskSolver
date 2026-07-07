"""HTTP/1.1 + SSE framing — provider-neutral wire decode.

Splits a captured plaintext byte stream into complete HTTP/1.1 ``Message`` objects
(content-length or chunked framing, gzip/br/deflate content-decode) and parses SSE
``text/event-stream`` bodies into their ``data:`` JSON objects. This is the framing layer
shared by every wrapper; the *provider* shaping (which endpoint is a model request, how a
response stream assembles into text/usage/model) lives in the provider package's turn
builder (e.g. ``pyagy.agy_process.http1sse`` for agy's genai shape).

Stdlib-only (brotli imported lazily), so it is safe to load inside the embedded interpreter.
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


def classify(direction, buf):
    """Sniff whether a plaintext stream is HTTP/1.1 (a request/response endpoint) or
    HTTP/2 (a gRPC connection). Returns ``"http1"``, ``"h2"``, or ``None`` (undecided —
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
