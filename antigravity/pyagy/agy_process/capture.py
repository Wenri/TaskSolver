"""Live request/response correlator for agy's model traffic.

The tls hooks hand us plaintext in two directions with a keying quirk: egress
(``crypto/tls.(*Conn).Write`` → ``tls_write``) is keyed by the ``*Conn`` address,
while ingress (``crypto/tls.(*halfConn).decrypt`` → ``tls_read``) is keyed by the
``*halfConn`` address — so the *same* logical connection shows up under two different
stream ids for its two directions. We therefore cannot pair a request with its
response by stream id; we pair by **time** (nearest preceding request) plus host.

Each stream is sniffed once (`http1sse.classify`) and routed:
  * HTTP/1.1  → ``http1sse.StreamDecoder`` (the model endpoint) → genai turns.
  * HTTP/2    → the existing ``h2reassemble.Reassembler`` (agy's gRPC connections).

Unlike offline decode from the capture JSONL, this runs *inside* agy with the full
plaintext bytes, so it emits complete ``genai_turn`` events regardless of the
recorder's preview limit.
"""
from . import http1sse


class Correlator:
    def __init__(self, recorder, reassembler=None):
        self.rec = recorder
        self.h2 = reassembler
        self._kind = {}          # (dir, stream) -> "http1" | "h2"
        self._pre = {}           # (dir, stream) -> bytearray (pre-classification buffer)
        self._dec = {}           # (dir, stream) -> StreamDecoder (http1 only)
        self._pending = []       # recent requests: [(t, host, requestId, msg)]

    def feed(self, direction, stream_id, data, t):
        # Ingress (s2c) is keyed by *halfConn and its decrypted byte stream begins with
        # a TLS-handshake plaintext prefix (ServerHello/Certificate/…) before the
        # HTTP/1.1 status line — so we can't sniff it by its opening bytes. The HTTP/1.1
        # decoder's header search skips that prefix, so we always run it on s2c. (The
        # model turn is HTTP/1.1; agy's h2 connections are handled on the c2s side.)
        if direction == "s2c":
            self._feed_http1(direction, stream_id, data, t)
            return
        # Egress (c2s) opens cleanly with a request line or the h2 preface — sniffable.
        key = (direction, stream_id)
        kind = self._kind.get(key)
        if kind is None:
            buf = self._pre.setdefault(key, bytearray())
            buf += data
            kind = http1sse.classify(direction, bytes(buf))
            if kind is None:
                return                      # need more bytes to tell http1 from h2
            self._kind[key] = kind
            data = bytes(buf)               # replay the buffered prefix into the router
            self._pre.pop(key, None)
        if kind == "h2":
            if self.h2 is not None:
                self.h2.feed(stream_id, direction, data)
            return
        self._feed_http1(direction, stream_id, data, t)

    def _feed_http1(self, direction, stream_id, data, t):
        dec = self._dec.get((direction, stream_id))
        if dec is None:
            dec = self._dec[(direction, stream_id)] = http1sse.StreamDecoder()
        for msg in dec.feed(data):
            if msg.is_request and http1sse.is_generate_content(msg.start_line):
                self._pending.append((t, msg.headers.get("host"), stream_id, msg))
                # keep only the recent tail
                if len(self._pending) > 32:
                    self._pending = self._pending[-32:]
            elif not msg.is_request and msg.is_event_stream:
                self._emit_turn(stream_id, t, msg)

    def _emit_turn(self, resp_stream, resp_t, resp_msg):
        req = self._match(resp_t, resp_msg.headers.get("host"))
        turn = http1sse.build_turn(
            (req[0], req[2], req[3]) if req else None,
            (resp_t, resp_stream, resp_msg),
        )
        self.rec.event(turn)

    def _match(self, resp_t, resp_host):
        """Nearest preceding request within a small time window; prefer same host."""
        best = None
        for entry in self._pending:
            qt, host, sid, msg = entry
            if qt > resp_t + 1.0:
                continue
            if best is None or qt > best[0]:
                if resp_host and host and host != resp_host and best is not None:
                    continue
                best = entry
        if best is not None:
            try:
                self._pending.remove(best)
            except ValueError:
                pass
        return best
