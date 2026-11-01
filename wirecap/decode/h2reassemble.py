"""Reassemble HTTP/2 messages from TLS-layer plaintext captures.

Each connection direction is reassembled independently:

    per (conn, direction):  bytes -> HTTP/2 frames -> per-h2-stream HEADERS+DATA

The optional `hpack` package decodes complete header blocks; DATA is still captured
without it. Content encodings are decoded through http1sse.inflate. gRPC payloads
remain raw, with their message lengths recorded for downstream protobuf decoding.

Capture must start with the connection to preserve HPACK's dynamic-table state.
"""
from typing import TypedDict

from . import http1sse          # shared content-decoding (inflate); stdlib-pure, no cycle
from ..types import Direction, Event, EventSink, StreamId

try:
    import hpack
except Exception:  # optional
    hpack = None

PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

FRAME_DATA = 0x0
FRAME_HEADERS = 0x1
FRAME_PUSH_PROMISE = 0x5
FRAME_CONTINUATION = 0x9
FLAG_END_STREAM = 0x1
FLAG_END_HEADERS = 0x4
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20


class _Stream(TypedDict):
    headers: list[tuple[str | bytes, str | bytes]]
    data: bytearray


class _HeaderBlock(TypedDict):
    sid: int
    data: bytearray
    end_stream: bool
    discard: bool


class _Side:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.dec = hpack.Decoder() if hpack else None
        self.saw_preface = False
        self.streams: dict[int, _Stream] = {}
        self.headers: _HeaderBlock | None = None
        self.failed = False


class Reassembler:
    def __init__(self, recorder: EventSink) -> None:
        self.rec = recorder
        self.conns: dict[StreamId, dict[Direction, _Side]] = {}

    def _side(self, conn: StreamId, direction: Direction) -> _Side:
        sides = self.conns.setdefault(conn, {})
        if direction not in sides:
            sides[direction] = _Side()
        return sides[direction]

    def feed(self, conn: StreamId, direction: Direction, data: bytes) -> None:
        side = self._side(conn, direction)
        if side.failed:
            return
        side.buf += data
        if direction == "c2s" and not side.saw_preface:
            if len(side.buf) < len(PREFACE):
                return
            if bytes(side.buf[:len(PREFACE)]) == PREFACE:
                del side.buf[:len(PREFACE)]
            side.saw_preface = True
        try:
            self._parse(conn, direction, side)
        except Exception:
            # Invalid frame sequences cannot be safely reassembled. Raw bytes
            # remain recorded, and this must never interrupt capture itself.
            side.failed = True
            side.buf.clear()
            side.streams.clear()
            side.headers = None

    def _parse(self, conn: StreamId, direction: Direction, side: _Side) -> None:
        b = side.buf
        while len(b) >= 9:
            length = (b[0] << 16) | (b[1] << 8) | b[2]
            if len(b) < 9 + length:
                break
            ftype, flags = b[3], b[4]
            sid = int.from_bytes(b[5:9], "big") & 0x7FFFFFFF
            payload = bytes(b[9:9 + length])
            del b[:9 + length]
            self._frame(conn, direction, side, ftype, flags, sid, payload)

    def _frame(self, conn: StreamId, direction: Direction, side: _Side,
               ftype: int, flags: int, sid: int, payload: bytes) -> None:
        pending: _HeaderBlock | None = side.headers
        if pending is not None and (ftype != FRAME_CONTINUATION or sid != pending["sid"]):
            raise ValueError("expected CONTINUATION for the unfinished header block")
        if ftype not in (FRAME_DATA, FRAME_HEADERS, FRAME_PUSH_PROMISE, FRAME_CONTINUATION):
            return
        if sid == 0:
            raise ValueError("HTTP/2 message frames require a nonzero stream ID")
        if ftype == FRAME_CONTINUATION and pending is None:
            raise ValueError("CONTINUATION without a header block")
        if ftype in (FRAME_HEADERS, FRAME_PUSH_PROMISE):
            block = _unpad(payload, flags)
            if ftype == FRAME_PUSH_PROMISE:
                if len(block) < 4 or not int.from_bytes(block[:4], "big") & 0x7FFFFFFF:
                    raise ValueError("invalid PUSH_PROMISE stream ID")
                block = block[4:]
            elif flags & FLAG_PRIORITY:
                if len(block) < 5:
                    raise ValueError("truncated HEADERS priority field")
                block = block[5:]
            pending = {"sid": sid, "data": bytearray(block),
                       "end_stream": bool(flags & FLAG_END_STREAM),
                       "discard": ftype == FRAME_PUSH_PROMISE}
            side.headers = pending
        elif ftype == FRAME_CONTINUATION:
            assert pending is not None
            pending["data"] += payload
        elif ftype == FRAME_DATA:
            st = side.streams.setdefault(sid, {"headers": [], "data": bytearray()})
            st["data"] += _unpad(payload, flags)
            if flags & FLAG_END_STREAM:
                self._emit(conn, direction, sid, st)
                side.streams.pop(sid, None)
            return
        if not flags & FLAG_END_HEADERS:
            return
        assert pending is not None
        side.headers = None
        headers: list[tuple[str | bytes, str | bytes]] = []
        if side.dec is not None:
            try:
                headers.extend(side.dec.decode(bytes(pending["data"])))
            except Exception:
                # A failed decode may mutate HPACK's dynamic table. Keep DATA,
                # but do not decode later blocks with an untrustworthy context.
                side.dec = None
        # Promised request headers share the connection's compression context,
        # but are not response headers for either the parent or promised stream.
        if pending["discard"]:
            return
        st = side.streams.setdefault(sid, {"headers": [], "data": bytearray()})
        st["headers"].extend(headers)
        if pending["end_stream"]:
            self._emit(conn, direction, sid, st)
            side.streams.pop(sid, None)

    def _emit(self, conn: StreamId, direction: Direction, sid: int, st: _Stream) -> None:
        headers: dict[str, str] = {}
        for k, v in st["headers"]:
            k = k.decode() if isinstance(k, bytes) else k
            v = v.decode("utf-8", "replace") if isinstance(v, bytes) else v
            headers[k] = v
        body = bytes(st["data"])
        # Same gzip/br/deflate handling as the HTTP/1.1 path — one implementation, in http1sse.
        # (brotli is Google's default here; brotli-python is a pixi dep, imported lazily there.)
        body = http1sse.inflate(body, headers.get("content-encoding", ""))
        msg: Event = {
            "kind": "h2msg", "conn": conn, "dir": direction, "h2sid": sid,
            "headers": headers, "body_len": len(body),
            "body_head": body[:256].hex(),
        }
        # gRPC: DATA is a sequence of [1-byte compressed flag][4-byte len][msg].
        if headers.get("content-type", "").startswith("application/grpc"):
            msg["grpc_frames"] = _grpc_lengths(body)
        self.rec.event(msg)


def _unpad(payload: bytes, flags: int) -> bytes:
    if not flags & FLAG_PADDED:
        return payload
    if not payload or payload[0] >= len(payload):
        raise ValueError("invalid HTTP/2 padding length")
    return payload[1:len(payload) - payload[0]]


def _grpc_lengths(body: bytes) -> list[int]:
    out: list[int] = []
    i = 0
    while i + 5 <= len(body):
        n = int.from_bytes(body[i + 1:i + 5], "big")
        out.append(n)
        i += 5 + n
        if len(out) > 256:
            break
    return out
