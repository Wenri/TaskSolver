"""Live request/response correlator — provider-neutral pairing + accumulation.

Two capture styles are supported, so both a raw-bytes source (an in-process shim handing us
plaintext off the wire) and a pre-parsed source (a patched CLI handing us decoded request /
stream-event JSON) drive the same machinery:

  * ``feed(direction, stream_id, data, t)`` — raw plaintext. Sniffed once per stream
    (``http1sse.classify``) and routed: HTTP/1.1 → per-stream ``StreamDecoder`` (requests
    tracked, event-stream responses assembled); HTTP/2 → the ``h2reassemble.Reassembler``.
  * ``feed_request(req_repr, t, ...)`` / ``feed_events(events, t)`` — already-parsed dicts.
  * ``feed_chunk(data, t)`` — a raw response chunk the CLI's own stream parser already decoded.

agy's ``tls_write`` entry book supplies requests; its SSE parser supplies response ``data:``
lines because decrypted reads return their bytes rather than receive them as entry arguments.
Codex supplies events from its patched HTTP boundary. Responses accumulate until a terminal
``finishReason`` / ``completed`` event and are paired with a pending request.

Request↔response pairing is by **time** (nearest preceding request within a small window,
preferring same host) — captures often key the two directions differently, so stream id
can't pair them; a builder that can tell (``TurnBuilder.request_matches``) narrows the
candidates first. ``TurnBuilder.stream_key`` keeps overlapping responses separate, including
agy's simultaneous session-title and answer streams. Provider-specific shaping stays in the builder.
"""
from collections.abc import Sequence
from typing import Literal

from . import http1sse
from .h2reassemble import Reassembler
from .turns import TurnBuilder
from ..types import Direction, EventSink, JSONObject, StreamId

type PendingRequest = tuple[float, str | None, StreamId, http1sse.Message | JSONObject | None]


class BaseCorrelator:
    def __init__(self, recorder: EventSink, builder: TurnBuilder,
                 reassembler: Reassembler | None = None) -> None:
        self.rec = recorder
        self._builder = builder
        self.h2 = reassembler
        self._kind: dict[tuple[Direction, StreamId], Literal["http1", "h2"]] = {}
        self._pre: dict[tuple[Direction, StreamId], bytearray] = {}
        self._dec: dict[tuple[Direction, StreamId], http1sse.StreamDecoder] = {}
        self._pending: list[PendingRequest] = []
        self._acc: dict[str | None, list[JSONObject]] = {}
        self._acc_t: dict[str | None, float] = {}

    # --- raw-bytes path (wire capture) ---------------------------------------
    def feed(self, direction: Direction, stream_id: StreamId, data: bytes, t: float) -> None:
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

    def _feed_http1(self, direction: Direction, stream_id: StreamId, data: bytes, t: float) -> None:
        dec = self._dec.get((direction, stream_id))
        if dec is None:
            dec = self._dec[(direction, stream_id)] = http1sse.StreamDecoder()
        for msg in dec.feed(data):
            if msg.is_request and self._builder.is_request(msg):
                self._flush_unkeyed()
                self._remember(t, msg.headers.get("host"), stream_id, msg)
            elif not msg.is_request and msg.is_event_stream:
                self._emit_message(stream_id, t, msg)

    # --- pre-parsed path (patched-CLI capture) -------------------------------
    def feed_request(self, req_repr: http1sse.Message | JSONObject | None, t: float,
                     host: str | None = None, stream_id: StreamId = None) -> None:
        """Track a pre-parsed request (e.g. a serialized request JSON) for pairing."""
        self._flush_unkeyed()
        self._remember(t, host, stream_id, req_repr)

    def _flush_unkeyed(self) -> None:
        """A new request means an unkeyed response is over: flush it if it never hit a terminal
        event (aborted stream), so its events can't bleed into the next turn. Keyed responses
        cannot bleed and may still be streaming, so they wait for their own terminal event."""
        if self._acc.get(None):
            self._flush_events(None)

    def feed_chunk(self, data: bytes, t: float) -> None:
        """Parse and accumulate a chunk supplied by the CLI's response parser."""
        self.feed_events(self._builder.parse_events(data), t)

    def feed_events(self, events: Sequence[JSONObject], t: float) -> None:
        """Accumulate already-parsed stream events per response (``TurnBuilder.stream_key``);
        emit a response's turn at its terminal event."""
        if not events:
            return
        groups: dict[str | None, list[JSONObject]] = {}
        for event in events:
            groups.setdefault(self._builder.stream_key(event), []).append(event)
        for key, group in groups.items():
            acc = self._acc.setdefault(key, [])
            if not acc:
                self._acc_t[key] = t
            acc.extend(group)
            if self._builder.is_terminal(acc):
                self._flush_events(key)

    def flush(self) -> None:
        """Emit every response still accumulating (a capture that ended mid-stream)."""
        for key in list(self._acc):
            if self._acc.get(key):
                self._flush_events(key)

    # --- turn emission --------------------------------------------------------
    def _flush_events(self, key: str | None = None) -> None:
        events = self._acc.pop(key, [])
        t = self._acc_t.pop(key, None)
        req = self._match(t, None, events)
        turn = self._builder.build_from_events(
            # resp_stream is None on this path: the accumulated-event route has no connection id
            # (resp_chunk's stream_id is a Go string pointer, not a conn — see agy_process).
            events, t, None,
            (req[0], req[2], req[3]) if req else None,
        )
        self.rec.event(turn)

    def _emit_message(self, resp_stream: StreamId, resp_t: float,
                      resp_msg: http1sse.Message) -> None:
        req = self._match(resp_t, resp_msg.headers.get("host"))
        turn = self._builder.build_from_message(
            (req[0], req[2], req[3]) if req else None, resp_t, resp_stream, resp_msg)
        self.rec.event(turn)

    def _remember(self, t: float, host: str | None, stream_id: StreamId,
                  req_repr: http1sse.Message | JSONObject | None) -> None:
        self._pending.append((t, host, stream_id, req_repr))
        if len(self._pending) > 32:                  # keep only the recent tail
            self._pending = self._pending[-32:]

    def _match(self, resp_t: float | None, resp_host: str | None,
               events: Sequence[JSONObject] | None = None) -> PendingRequest | None:
        """Nearest preceding request within a small time window; prefer same host. Given the
        response ``events``, the builder's ``request_matches`` narrows the candidates first: to
        the requests it accepts if any, else to those it has no verdict on; if it rejects every
        one, time alone decides."""
        if resp_t is None:
            return None
        candidates = self._pending
        if events:
            verdicts = [self._builder.request_matches(entry[3], events) for entry in candidates]
            for keep in (True, None):
                if keep in verdicts:
                    candidates = [e for e, v in zip(candidates, verdicts) if v is keep]
                    break
        best: PendingRequest | None = None
        for entry in candidates:
            qt, host, _, _ = entry
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
