"""Reassemble HTTP/2 messages from TLS-layer plaintext captures.

The `crypto/tls.(*Conn).Write/Read` hooks give us the *plaintext* byte stream to
and from the backend — which for agy is HTTP/2 (gRPC: `content-type:
application/grpc`, protobuf payloads). This module reassembles that stream:

    per (conn, direction):  bytes -> HTTP/2 frames -> per-h2-stream HEADERS+DATA

HEADERS are HPACK-decoded if the `hpack` package is installed (recommended:
`pip install hpack`); without it, DATA is still captured. gzip bodies are
inflated. gRPC length-prefixed messages inside DATA are left raw (decode with
your .proto downstream) but split out for convenience.

Because we hook from process start, we see each connection from its HTTP/2
preface, so the HPACK dynamic table stays in sync.
"""
import gzip
import struct

try:
    import hpack
except Exception:  # optional
    hpack = None

PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

FRAME_DATA = 0x0
FRAME_HEADERS = 0x1
FRAME_CONTINUATION = 0x9
FLAG_END_STREAM = 0x1
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20


class _Side:
    def __init__(self):
        self.buf = bytearray()
        self.dec = hpack.Decoder() if hpack else None
        self.saw_preface = False
        self.streams = {}       # h2 stream id -> {headers, data, pending_hdr}


class Reassembler:
    def __init__(self, recorder):
        self.rec = recorder
        self.conns = {}         # conn_id -> {"c2s": _Side, "s2c": _Side}

    def _side(self, conn, direction):
        return self.conns.setdefault(conn, {}).setdefault(direction, _Side())

    def feed(self, conn, direction, data):
        side = self._side(conn, direction)
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
            # Never let reassembly break capture; raw bytes are already recorded.
            pass

    def _parse(self, conn, direction, side):
        b = side.buf
        while len(b) >= 9:
            length = (b[0] << 16) | (b[1] << 8) | b[2]
            if len(b) < 9 + length:
                break
            ftype, flags = b[3], b[4]
            sid = struct.unpack(">I", bytes(b[5:9]))[0] & 0x7FFFFFFF
            payload = bytes(b[9:9 + length])
            del b[:9 + length]
            self._frame(conn, direction, side, ftype, flags, sid, payload)

    def _frame(self, conn, direction, side, ftype, flags, sid, payload):
        st = side.streams.setdefault(sid, {"headers": [], "data": bytearray()})
        if ftype == FRAME_HEADERS:
            block = payload
            if flags & FLAG_PADDED:
                pad = block[0]
                block = block[1:len(block) - pad]
            if flags & FLAG_PRIORITY:
                block = block[5:]
            if side.dec is not None:
                try:
                    st["headers"].extend(side.dec.decode(block))
                except Exception:
                    pass
        elif ftype == FRAME_DATA:
            data = payload
            if flags & FLAG_PADDED:
                pad = data[0]
                data = data[1:len(data) - pad]
            st["data"] += data
        if (flags & FLAG_END_STREAM) and ftype in (FRAME_DATA, FRAME_HEADERS):
            self._emit(conn, direction, sid, st)
            side.streams.pop(sid, None)

    def _emit(self, conn, direction, sid, st):
        headers = {}
        for k, v in st["headers"]:
            k = k.decode() if isinstance(k, bytes) else k
            v = v.decode("utf-8", "replace") if isinstance(v, bytes) else v
            headers[k] = v
        body = bytes(st["data"])
        enc = headers.get("content-encoding", "")
        if "gzip" in enc:
            try:
                body = gzip.decompress(body)
            except Exception:
                pass
        elif "br" in enc:                       # brotli (Google's default) — needs `pip install brotli`
            try:
                import brotli
                body = brotli.decompress(body)
            except Exception:
                pass
        elif "deflate" in enc:
            try:
                import zlib
                body = zlib.decompress(body)
            except Exception:
                pass
        msg = {
            "kind": "h2msg", "conn": conn, "dir": direction, "h2sid": sid,
            "headers": headers, "body_len": len(body),
            "body_head": body[:256].hex(),
        }
        # gRPC: DATA is a sequence of [1-byte compressed flag][4-byte len][msg].
        if headers.get("content-type", "").startswith("application/grpc"):
            msg["grpc_frames"] = _grpc_lengths(body)
        self.rec.event(msg)


def _grpc_lengths(body):
    out, i = [], 0
    while i + 5 <= len(body):
        n = struct.unpack(">I", body[i + 1:i + 5])[0]
        out.append(n)
        i += 5 + n
        if len(out) > 256:
            break
    return out
