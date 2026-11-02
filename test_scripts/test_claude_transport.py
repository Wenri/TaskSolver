#!/usr/bin/env python3
"""Offline Unix-socket framing and ownership checks for the native UI transport."""

from __future__ import annotations

import asyncio
import errno
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pyclaude import attach_terminal
from pyclaude.transport import NativeTransport
from wirecap.types import json_loads_object


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tasksolver-claude-transport-")
        self.path = Path(self.temporary.name) / "native.sock"
        self.server: asyncio.Server | None = None
        self.connections: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = asyncio.Queue()
        self.writers: list[asyncio.StreamWriter] = []
        self.transports: list[NativeTransport] = []

    async def asyncTearDown(self) -> None:
        if self.server is not None:
            self.server.close()
        for transport in self.transports:
            await transport.close()
        if self.server is not None:
            self.server.close_clients()
        for writer in self.writers:
            writer.close()
        for writer in self.writers:
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass
        if self.server is not None:
            await asyncio.wait_for(self.server.wait_closed(), 2)
        self.temporary.cleanup()

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        self.connections.put_nowait((reader, writer))

    async def start_server(self) -> None:
        self.server = await asyncio.start_unix_server(self.accept, path=self.path, limit=2 * 1024 * 1024)

    def transport(self, *, connect_timeout: float = 1, max_message_bytes: int = 1024 * 1024) -> NativeTransport:
        transport = NativeTransport(self.path, connect_timeout=connect_timeout,
                                    max_message_bytes=max_message_bytes)
        self.transports.append(transport)
        return transport

    async def connect(self, *, max_message_bytes: int = 1024 * 1024) -> tuple[
        NativeTransport, asyncio.StreamReader, asyncio.StreamWriter
    ]:
        transport = self.transport(max_message_bytes=max_message_bytes)
        await transport.connect()
        reader, writer = await asyncio.wait_for(self.connections.get(), 1)
        return transport, reader, writer

    async def test_fragmented_utf8_and_coalesced_messages(self) -> None:
        await self.start_server()
        transport, _, writer = await self.connect()
        first = {"type": "message", "text": "café 🦊"}
        encoded = json.dumps(first, ensure_ascii=False).encode()
        cut = encoded.index("🦊".encode()) + 2
        messages = transport.read_messages()
        pending = asyncio.ensure_future(anext(messages))
        writer.write(encoded[:cut])
        await writer.drain()
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        writer.write(encoded[cut:] + b'\n{"type":"next"}\n')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(pending, 1), first)
        self.assertEqual(await asyncio.wait_for(anext(messages), 1), {"type": "next"})
        await transport.close()
        with self.assertRaises(StopAsyncIteration):
            await anext(messages)

    async def test_byte_limit_excludes_newline_and_counts_utf8_bytes(self) -> None:
        await self.start_server()
        payload = json.dumps({"text": "🦊"}, ensure_ascii=False).encode()
        transport, _, writer = await self.connect(max_message_bytes=len(payload))
        writer.write(payload + b"\n")
        await writer.drain()
        messages = transport.read_messages()
        self.assertEqual(await asyncio.wait_for(anext(messages), 1), {"text": "🦊"})
        writer.write(payload + b" \n")
        await writer.drain()
        with self.assertRaises(ValueError):
            await asyncio.wait_for(anext(messages), 1)
        self.assertFalse(transport.is_ready())

    async def test_oversized_unterminated_frame_is_rejected(self) -> None:
        await self.start_server()
        transport, _, writer = await self.connect(max_message_bytes=32)
        writer.write(b'{"text":"' + b"x" * 33)
        await writer.drain()
        with self.assertRaises(ValueError):
            await asyncio.wait_for(anext(transport.read_messages()), 1)
        self.assertFalse(transport.is_ready())

    async def test_invalid_json_nonobjects_and_bridge_errors_fail_explicitly(self) -> None:
        await self.start_server()
        for frame, exception in ((b"{\n", ValueError), (b"[]\n", ValueError),
                                 (b"null\n", ValueError),
                                 (b'{"type":"tasksolver_error","error":"native UI is busy"}\n', RuntimeError)):
            with self.subTest(frame=frame):
                transport, _, writer = await self.connect()
                writer.write(frame)
                await writer.drain()
                with self.assertRaises(exception) as caught:
                    await asyncio.wait_for(anext(transport.read_messages()), 1)
                if exception is RuntimeError:
                    self.assertIn("native UI is busy", str(caught.exception))
                self.assertFalse(transport.is_ready())

    async def test_clean_eof_and_truncated_eof_are_distinct(self) -> None:
        await self.start_server()
        for partial in (b"", b'{"text":"unfinished'):
            with self.subTest(partial=partial):
                transport, _, writer = await self.connect()
                writer.write(partial)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                with self.assertRaises(EOFError if partial else ConnectionError):
                    await asyncio.wait_for(anext(transport.read_messages()), 1)
                self.assertFalse(transport.is_ready())
                with self.assertRaises(RuntimeError):
                    await transport.connect()

    async def test_concurrent_writes_preserve_complete_frames(self) -> None:
        await self.start_server()
        transport, reader, _ = await self.connect()
        frames = [{"index": index, "text": "🦊" * 8192} for index in range(12)]

        async def receive() -> list[dict[str, object]]:
            decoded: list[dict[str, object]] = []
            for _ in frames:
                line = await reader.readline()
                decoded.append(dict(json_loads_object(line)))
            return decoded

        receive_task = asyncio.create_task(receive())
        await asyncio.wait_for(asyncio.gather(*(
            transport.write(json.dumps(frame, ensure_ascii=False) + "\n") for frame in frames
        )), 5)
        received = await asyncio.wait_for(receive_task, 5)
        self.assertEqual({frame["index"] for frame in received}, set(range(12)))
        self.assertTrue(all(frame["text"] == "🦊" * 8192 for frame in received))

    async def test_invalid_outgoing_frames_never_reach_server(self) -> None:
        await self.start_server()
        transport, reader, _ = await self.connect(max_message_bytes=32)
        for frame in ('{"unfinished":1}', "[]\n", "bad\n", '{"text":"' + "x" * 40 + '"}\n'):
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                await transport.write(frame)
        await transport.write('{"valid":true}\n')
        self.assertEqual(await asyncio.wait_for(reader.readline(), 1), b'{"valid":true}\n')

    async def test_connect_retries_until_listener_is_ready(self) -> None:
        transport = self.transport()
        pending = asyncio.create_task(transport.connect())
        await asyncio.sleep(0.03)
        self.assertFalse(pending.done())
        await self.start_server()
        await asyncio.wait_for(pending, 1)
        self.assertTrue(transport.is_ready())

    async def test_connect_timeout_can_be_retried(self) -> None:
        transport = self.transport(connect_timeout=0.03)
        with self.assertRaises(TimeoutError):
            await transport.connect()
        self.assertFalse(transport.is_ready())
        await self.start_server()
        await transport.connect()
        self.assertTrue(transport.is_ready())

    async def test_permission_failure_is_not_retried(self) -> None:
        transport = self.transport()
        with patch.object(NativeTransport, "_open_socket",
                          side_effect=PermissionError(errno.EACCES, "denied")) as connect:
            start = time.monotonic()
            with self.assertRaises(PermissionError):
                await transport.connect()
            self.assertLess(time.monotonic() - start, 0.5)
            connect.assert_called_once()

    async def test_end_input_and_close_preserve_server_ownership(self) -> None:
        await self.start_server()
        transport, reader, _ = await self.connect()
        await transport.connect()
        self.assertTrue(self.connections.empty())
        await transport.end_input()
        await transport.write('{"still":"connected"}\n')
        self.assertEqual(await asyncio.wait_for(reader.readline(), 1), b'{"still":"connected"}\n')
        await transport.close()
        await transport.close()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertFalse(transport.is_ready())
        with self.assertRaises(RuntimeError):
            await transport.connect()
        second, _, _ = await self.connect()
        self.assertTrue(second.is_ready())

    async def test_cancelled_connect_and_read_can_be_closed(self) -> None:
        connecting = self.transport()
        pending_connect = asyncio.create_task(connecting.connect())
        await asyncio.sleep(0)
        pending_connect.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending_connect
        await connecting.close()
        await self.start_server()
        transport, reader, _ = await self.connect()
        pending_read = asyncio.ensure_future(anext(transport.read_messages()))
        await asyncio.sleep(0)
        pending_read.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending_read
        self.assertFalse(transport.is_ready())
        await transport.close()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")

    async def test_cancelling_a_partial_write_closes_the_connection(self) -> None:
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        errors: list[dict[str, object]] = []

        def unexpected_error(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
            errors.append(context)

        loop.set_exception_handler(unexpected_error)
        self.addCleanup(loop.set_exception_handler, previous_handler)
        await self.start_server()
        transport, reader, _ = await self.connect(max_message_bytes=16 * 1024 * 1024)
        pending = asyncio.create_task(transport.write('{"text":"' + "x" * (8 * 1024 * 1024) + '"}\n'))
        await asyncio.sleep(0.03)
        self.assertFalse(pending.done(), "Fixture must hold the socket write under backpressure")
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(transport.is_ready())
        partial = await asyncio.wait_for(reader.read(), 2)
        self.assertTrue(partial)
        self.assertFalse(partial.endswith(b"\n"))
        with self.assertRaises(ConnectionError):
            await transport.write('{"next":true}\n')
        await asyncio.sleep(0)
        self.assertEqual(errors, [], "Socket cancellation must not leave failing event-loop callbacks")

    async def test_close_cancels_an_in_progress_connection(self) -> None:
        transport = self.transport()
        connecting = asyncio.create_task(transport.connect())
        await asyncio.sleep(0.01)
        await transport.close()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(connecting, 1)
        self.assertFalse(transport.is_ready())

    def test_invalid_limits_are_rejected(self) -> None:
        for timeout in (True, 0, -1, math.inf, math.nan):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                NativeTransport(self.path, connect_timeout=timeout)
        for size in (True, 0, -1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                NativeTransport(self.path, max_message_bytes=size)

    def test_attach_factory_rejects_options_for_launching_a_different_process(self) -> None:
        self.assertRaises(TypeError, attach_terminal, self.path, model="another-model")
        self.assertRaises(TypeError, attach_terminal, self.path, sdk_options={"tools": []})


if __name__ == "__main__":
    unittest.main()
