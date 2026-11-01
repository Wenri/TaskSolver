"""HTTP/1.1 + SSE framing — provider-neutral wire decode.

Splits a captured plaintext byte stream into complete HTTP/1.1 ``Message`` objects
(content-length or chunked framing, gzip/br/deflate content-decode) and parses SSE
``text/event-stream`` bodies into their ``data:`` JSON objects. This is the framing layer
shared by every wrapper; the *provider* shaping (which endpoint is a model request, how a
response stream assembles into text/usage/model) lives in the provider package's turn
builder (e.g. ``pyagy.agy_process.http1sse`` for agy's genai shape).

Stdlib-only (brotli imported lazily), so it is safe to load inside the embedded interpreter.
"""
import re
import zlib
from typing import Literal

from ..types import Direction, JSONObject, json_loads

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

    def __init__(self, start_line: str, headers: dict[str, str], body: bytes) -> None:
        self.start_line = start_line
        self.headers = headers
        self.body = body

    @property
    def is_request(self) -> bool:
        return not self.start_line.startswith("HTTP/1.1")

    @property
    def method(self) -> str | None:
        return self.start_line.split(" ", 1)[0] if self.is_request else None

    @property
    def path(self) -> str | None:
        parts = self.start_line.split(" ")
        return parts[1] if self.is_request and len(parts) > 1 else None

    @property
    def status(self) -> int | None:
        if self.is_request:
            return None
        parts = self.start_line.split(" ")
        try:
            return int(parts[1])
        except (IndexError, ValueError):
            return None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def is_event_stream(self) -> bool:
        return "event-stream" in self.content_type or self.body[:6] == b"data: "

    def __repr__(self) -> str:
        return f"<Message {self.start_line!r} ctype={self.content_type!r} body={len(self.body)}B>"


def parse_headers(head_bytes: bytes) -> tuple[str, dict[str, str]]:
    """Split a raw header block (start line + fields, no trailing CRLFCRLF) into
    ``(start_line, {lowercased_name: value})``."""
    lines = head_bytes.decode("latin1").split("\r\n")
    headers: dict[str, str] = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return lines[0], headers


def dechunk(buf: bytes | bytearray, start: int) -> tuple[bytes | None, int]:
    """De-chunk ``transfer-encoding: chunked`` starting at ``buf[start:]``.
    Returns ``(decoded_bytes, consumed)`` where ``consumed`` counts bytes from
    ``start`` through the final trailer CRLF, or ``(None, 0)`` for incomplete or
    malformed framing."""
    out = bytearray()
    i = start
    n_buf = len(buf)
    while True:
        j = buf.find(b"\r\n", i)
        if j < 0:
            return None, 0
        size_text, separator, _ = bytes(buf[i:j]).partition(b";")
        if separator:
            size_text = size_text.rstrip(b" \t")  # RFC 9112 permits BWS before extensions.
        if re.fullmatch(rb"[0-9a-fA-F]+", size_text) is None:
            return None, 0
        size = int(size_text, 16)
        i = j + 2
        if size == 0:
            # Trailers belong to this message; only an empty line closes them.
            while True:
                end = buf.find(b"\r\n", i)
                if end < 0:
                    return None, 0
                if end == i:
                    return bytes(out), (end + 2) - start
                i = end + 2
        if n_buf - i < size + 2:
            return None, 0
        if buf[i + size:i + size + 2] != b"\r\n":
            return None, 0
        out += buf[i:i + size]
        i += size + 2


def inflate(body: bytes, encoding: str) -> bytes:
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


def parse_sse(body: bytes) -> list[JSONObject]:
    """Parse JSON event data, joining an event's ``data:`` fields with newlines.

    Accept a final event without its blank separator: native response books also
    pass individual, already-delimited SSE lines here. ``[DONE]`` is skipped.
    """
    events: list[JSONObject] = []
    data: list[bytes] = []
    for line in [*re.split(rb"\r\n|\r|\n", body.removeprefix(b"\xef\xbb\xbf")), b""]:
        if line:
            field, _, value = line.partition(b":")
            if field == b"data":
                data.append(value.removeprefix(b" "))
            continue
        payload = b"\n".join(data)
        data.clear()
        if payload and payload != b"[DONE]":
            try:
                event = json_loads(payload)
                if isinstance(event, dict):
                    events.append(event)
            except ValueError:
                pass
    return events


class StreamDecoder:
    """Accumulate one direction's plaintext byte stream and yield complete
    ``Message`` objects as their framing closes. Feed bytes in order via ``feed``;
    iterate the returned messages."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0            # scan start: bytes before here are consumed/discarded

    def feed(self, data: bytes) -> list[Message]:
        """Append ``data`` and return a list of newly-completed ``Message``s."""
        self.buf += data
        done: list[Message] = []
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

    def _try_one(self) -> tuple[Message | None, int]:
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
            length = headers["content-length"]
            if re.fullmatch(r"[0-9]+", length) is None:
                return None, self.pos
            try:
                n = int(length)
            except ValueError:
                return None, self.pos
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


def classify(direction: Direction, buf: bytes) -> Literal["http1", "h2"] | None:
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
