#!/usr/bin/env python3
"""Offline SSE, chunked HTTP/1.1, and HTTP/2 framing regressions."""
from collections.abc import Mapping
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hpack

from wirecap.decode import h2reassemble as h2
from wirecap.decode import http1sse as h1
from wirecap.types import Event


def frame(kind: int, flags: int, sid: int, payload: bytes = b"") -> bytes:
    return len(payload).to_bytes(3, "big") + bytes((kind, flags)) + sid.to_bytes(4, "big") + payload


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def event(self, obj: Mapping[str, object]) -> None:
        self.events.append(dict(obj))


class HTTP1FramingTests(unittest.TestCase):
    def test_multiline_sse_data_is_one_json_event(self) -> None:
        body = (b': heartbeat\nevent: response\ndata: {"text":\n'
                b'data: "hello",\ndata: "tokens": 2}\n\n'
                b'data: [DONE]\n\n')
        self.assertEqual(h1.parse_sse(body), [{"text": "hello", "tokens": 2}])

    def test_sse_fields_are_not_independent_json_documents(self) -> None:
        self.assertEqual(h1.parse_sse(b'data: {"a": 1}\ndata: {"b": 2}\n\n'), [])
        self.assertEqual(h1.parse_sse(b' data: {"a": 1}\n\n'), [])

    def test_sse_bom_and_all_line_endings(self) -> None:
        for newline in (b"\n", b"\r\n", b"\r"):
            with self.subTest(newline=newline):
                body = b"\xef\xbb\xbf" + newline.join((b'data: {"ok":', b'data: true}', b"", b""))
                self.assertEqual(h1.parse_sse(body), [{"ok": True}])

    def test_native_sse_line_without_separator_remains_supported(self) -> None:
        self.assertEqual(h1.parse_sse(b'data: {"text": "native chunk"}'),
                         [{"text": "native chunk"}])

    def test_chunk_trailers_require_final_blank_line(self) -> None:
        chunked = b"4;name=value\r\ndata\r\n0\r\nDigest: first\r\nOther: second\r\n\r\n"
        for end in range(len(chunked)):
            with self.subTest(end=end):
                self.assertEqual(h1.dechunk(chunked[:end], 0), (None, 0))
        self.assertEqual(h1.dechunk(b"prefix" + chunked + b"next", 6), (b"data", len(chunked)))

    def test_chunk_size_and_data_terminator_are_validated(self) -> None:
        for size in (b"+1", b"-0", b"0x1", b"1_0", b" 1", b"1 "):
            with self.subTest(size=size):
                self.assertEqual(h1.dechunk(size + b"\r\nx\r\n0\r\n\r\n", 0), (None, 0))
        self.assertEqual(h1.dechunk(b"1\r\nxZZ0\r\n\r\n", 0), (None, 0))
        self.assertEqual(h1.dechunk(b"1 \t;ext=yes\r\nx\r\n0\r\n\r\n", 0)[0], b"x")

    def test_chunked_trailers_preserve_following_message(self) -> None:
        first = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                 b"1\r\nx\r\n0\r\nDigest: first\r\nOther: second\r\n\r\n")
        second = b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\ny"
        decoder = h1.StreamDecoder()
        self.assertEqual(decoder.feed(first[:-2]), [])
        messages = decoder.feed(first[-2:] + second)
        self.assertEqual([message.body for message in messages], [b"x", b"y"])

    def test_invalid_content_length_never_emits_or_moves_backwards(self) -> None:
        for length in (b"-1000", b"+1", b"1_0", b"not-a-length", b""):
            with self.subTest(length=length):
                decoder = h1.StreamDecoder()
                self.assertEqual(decoder.feed(b"HTTP/1.1 200 OK\r\nContent-Length: "
                                              + length + b"\r\n\r\nx"), [])
                self.assertEqual(decoder.pos, 0)


class HTTP2FramingTests(unittest.TestCase):
    def test_end_stream_waits_for_continuation_and_dynamic_table(self) -> None:
        recorder = Recorder()
        decoder = h2.Reassembler(recorder)
        encoder = hpack.Encoder()
        headers = {":status": "200", "x-first": "one", "x-last": "a long header value" * 20}
        first = encoder.encode(headers.items(), huffman=False)
        later = encoder.encode(headers.items(), huffman=False)
        decoder.feed(1, "s2c", frame(1, 1, 1, first[:-3]))
        self.assertEqual(recorder.events, [])
        decoder.feed(1, "s2c", frame(9, 0, 1, first[-3:-1]))
        self.assertEqual(recorder.events, [])
        decoder.feed(1, "s2c", frame(9, 4, 1, first[-1:]) + frame(1, 5, 3, later))
        self.assertEqual([event["headers"] for event in recorder.events], [headers, headers])
        self.assertEqual([event["h2sid"] for event in recorder.events], [1, 3])

    def test_fragmented_padded_priority_headers_data_and_trailers(self) -> None:
        recorder = Recorder()
        decoder = h2.Reassembler(recorder)
        encoder = hpack.Encoder()
        headers = encoder.encode({":status": "200", "content-type": "application/grpc"})
        trailers = encoder.encode({"grpc-status": "0", "x-trailer": "finished"})
        body = b"\x00\x00\x00\x00\x03abc"
        wire = (frame(1, 0x28, 1, b"\x02" + b"\x00" * 5 + headers[:2] + b"\x00\x00")
                + frame(9, 4, 1, headers[2:])
                + frame(0, 8, 1, b"\x01" + body + b"\x00")
                + frame(1, 1, 1, trailers[:2]) + frame(9, 4, 1, trailers[2:]))
        for byte in wire:
            decoder.feed("conn", "s2c", bytes((byte,)))
        self.assertEqual(len(recorder.events), 1)
        self.assertEqual(recorder.events[0]["headers"], {
            ":status": "200", "content-type": "application/grpc",
            "grpc-status": "0", "x-trailer": "finished",
        })
        self.assertEqual(recorder.events[0]["body_head"], body.hex())
        self.assertEqual(recorder.events[0]["grpc_frames"], [3])

    def test_decoder_is_created_once_per_connection_direction(self) -> None:
        recorder = Recorder()
        with patch.object(h2.hpack, "Decoder", wraps=hpack.Decoder) as constructor:
            decoder = h2.Reassembler(recorder)
            for byte in h2.PREFACE + frame(4, 0, 0):
                decoder.feed("conn", "c2s", bytes((byte,)))
            constructor.assert_called_once()

    def test_push_promise_updates_shared_hpack_state_without_emitting(self) -> None:
        recorder = Recorder()
        decoder = h2.Reassembler(recorder)
        encoder = hpack.Encoder()
        promised = encoder.encode({":method": "GET", ":path": "/pushed", "x-shared": "value"})
        response = encoder.encode({":status": "200", "x-shared": "value"})
        decoder.feed(1, "s2c", frame(5, 0, 1, (2).to_bytes(4, "big") + promised[:3])
                     + frame(9, 4, 1, promised[3:]) + frame(1, 5, 2, response))
        self.assertEqual(len(recorder.events), 1)
        self.assertEqual(recorder.events[0]["headers"], {":status": "200", "x-shared": "value"})
        self.assertEqual(recorder.events[0]["h2sid"], 2)

    def test_invalid_continuation_and_padding_do_not_emit_messages(self) -> None:
        block = hpack.Encoder().encode({":status": "200"})
        invalid = (
            frame(1, 0, 1, block) + frame(9, 4, 3),
            frame(1, 0, 1, block) + frame(0, 1, 1, b"interleaved"),
            frame(9, 4, 1, block),
            frame(1, 5, 0, block),
            frame(1, 0x0D, 1, b""),
            frame(1, 0x0D, 1, b"\x03xx"),
            frame(1, 0x25, 1, b"short"[:4]),
            frame(0, 0x09, 1, b"\x01"),
        )
        for wire in invalid:
            with self.subTest(wire=wire):
                recorder = Recorder()
                decoder = h2.Reassembler(recorder)
                decoder.feed(1, "s2c", wire)
                decoder.feed(1, "s2c", frame(0, 1, 1, b"after invalid framing"))
                self.assertEqual(recorder.events, [])

    def test_data_is_captured_without_optional_hpack(self) -> None:
        recorder = Recorder()
        with patch.object(h2, "hpack", None):
            decoder = h2.Reassembler(recorder)
            decoder.feed(1, "s2c", frame(1, 0, 1, b"x") + frame(9, 4, 1, b"y")
                         + frame(0, 1, 1, b"body"))
        self.assertEqual(len(recorder.events), 1)
        self.assertEqual(recorder.events[0]["headers"], {})
        self.assertEqual(recorder.events[0]["body_head"], b"body".hex())


if __name__ == "__main__":
    unittest.main(verbosity=2)
