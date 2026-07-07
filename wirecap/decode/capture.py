"""Live request/response correlator — provider-neutral pairing + accumulation.

Two capture styles are supported, so both a raw-bytes source (an in-process shim handing us
plaintext off the wire) and a pre-parsed source (a patched CLI handing us decoded request /
stream-event JSON) drive the same machinery:

  * ``feed(direction, stream_id, data, t)`` — raw plaintext. Sniffed once per stream
    (``http1sse.classify``) and routed: HTTP/1.1 → per-stream ``StreamDecoder`` (requests
    tracked, event-stream responses assembled); HTTP/2 → the ``h2reassemble.Reassembler``.
  * ``feed_request(req_repr, t, ...)`` / ``feed_events(events, t)`` — already-parsed dicts.

Request↔response pairing is by **time** (nearest preceding request within a small window,
preferring same host) — captures often key the two directions differently, so stream id
can't pair them. All provider-specific shaping is delegated to a ``TurnBuilder``.
"""
from . import http1sse


class BaseCorrelator:
    def __init__(self, recorder, builder, reassembler=None):
        self.rec = recorder
        self._builder = builder
        self.h2 = reassembler
        self._kind = {}          # (dir, stream) -> "http1" | "h2"
        self._pre = {}           # (dir, stream) -> bytearray (pre-classification buffer)
        self._dec = {}           # (dir, stream) -> StreamDecoder (http1 only)
        self._pending = []       # recent requests: [(t, host, stream_id, req_repr)]
        self._acc = []           # stream events accumulated for the in-flight response
        self._acc_t = None       # timestamp of the first accumulated event

    # --- raw-bytes path (wire capture) ---------------------------------------
    def feed(self, direction, stream_id, data, t):
        # Ingress (s2c) may begin with a TLS-handshake plaintext prefix before the HTTP/1.1
        # status line — the decoder's header search skips it — so we always run the HTTP/1.1
        # decoder on s2c. Egress (c2s) opens cleanly with a request line or the h2 preface, so
        # it is sniffable (http1 vs h2).
        if direction == "s2c":
            self._feed_http1(direction, stream_id, data, t)
            return
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
            if msg.is_request and self._builder.is_request(msg):
                # A new request means the previous response is over: flush it if it never hit
                # a terminal event (aborted stream), so its events can't bleed into this turn.
                if self._acc:
                    self._flush_events(None)
                self._remember(t, msg.headers.get("host"), stream_id, msg)
            elif not msg.is_request and msg.is_event_stream:
                self._emit_message(stream_id, t, msg)

    # --- pre-parsed path (patched-CLI capture) -------------------------------
    def feed_request(self, req_repr, t, host=None, stream_id=None):
        """Track a pre-parsed request (e.g. a serialized request JSON) for pairing."""
        if self._acc:
            self._flush_events(None)
        self._remember(t, host, stream_id, req_repr)

    def feed_events(self, events, t):
        """Accumulate already-parsed stream events; emit the turn at the terminal event."""
        if not events:
            return
        if not self._acc:
            self._acc_t = t
        self._acc.extend(events)
        if self._builder.is_terminal(self._acc):
            self._flush_events(None)

    # --- turn emission --------------------------------------------------------
    def _flush_events(self, resp_stream):
        req = self._match(self._acc_t, None)
        turn = self._builder.build_from_events(
            self._acc, self._acc_t, resp_stream,
            (req[0], req[2], req[3]) if req else None,
        )
        self._acc = []
        self._acc_t = None
        self.rec.event(turn)

    def _emit_message(self, resp_stream, resp_t, resp_msg):
        req = self._match(resp_t, resp_msg.headers.get("host"))
        turn = self._builder.build_from_message(
            (req[0], req[2], req[3]) if req else None, resp_t, resp_stream, resp_msg)
        self.rec.event(turn)

    def _remember(self, t, host, stream_id, req_repr):
        self._pending.append((t, host, stream_id, req_repr))
        if len(self._pending) > 32:                  # keep only the recent tail
            self._pending = self._pending[-32:]

    def _match(self, resp_t, resp_host):
        """Nearest preceding request within a small time window; prefer same host."""
        best = None
        for entry in self._pending:
            qt, host, sid, req = entry
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
