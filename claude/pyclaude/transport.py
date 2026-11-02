"""SDK transport for one connection to Claude's native UI bridge."""

from __future__ import annotations

import errno
import math
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from anyio.lowlevel import checkpoint

from wirecap.types import JSONObject, json_loads_object


class NativeTransport:
    """Attach the SDK to a private Unix socket without owning the Claude process.

    The native UI must already run from TaskSolver's bundled runtime. SDK
    launch options are not applied by a custom transport; configure the UI when
    launching it. Each transport owns one socket connection.

    Closing, peer EOF, or a protocol/I/O failure ends this instance permanently;
    create a new instance to reconnect. A failed initial connection may be retried.
    Message limits count UTF-8 bytes excluding the terminating newline. Outgoing
    writes must contain complete JSON object frames and fit within the same cap.
    """

    def __init__(self, path: str | os.PathLike[str], *, connect_timeout: float = 10.0,
                 max_message_bytes: int = 16 * 1024 * 1024) -> None:
        if (isinstance(connect_timeout, bool) or not math.isfinite(connect_timeout)
                or connect_timeout <= 0):
            raise ValueError("connect_timeout must be finite and positive")
        if type(max_message_bytes) is not int or max_message_bytes <= 0:
            raise ValueError("max_message_bytes must be a positive integer")
        raw_path = os.fspath(path)
        if not raw_path or "\0" in raw_path:
            raise ValueError("A filesystem Unix socket path is required")
        self.path: Path = Path(raw_path).absolute()
        self.connect_timeout: float = connect_timeout
        self.max_message_bytes: int = max_message_bytes
        self._socket: socket.socket | None = None
        self._closed: bool = False
        self._reading: bool = False
        self._connect_lock: anyio.Lock = anyio.Lock()
        self._write_lock: anyio.Lock = anyio.Lock()
        self._connecting_scope: anyio.CancelScope | None = None

    def is_ready(self) -> bool:
        return self._socket is not None and not self._closed

    async def _open_socket(self) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.setblocking(False)
        try:
            while True:
                await checkpoint()
                try:
                    connection.connect(str(self.path))
                except BlockingIOError:
                    await anyio.wait_writable(connection)
                else:
                    return connection
        except BaseException:
            anyio.notify_closing(connection)
            connection.close()
            raise

    async def connect(self) -> None:
        """Connect, retrying only a missing socket or a listener not yet ready."""
        async with self._connect_lock:
            if self._closed:
                raise RuntimeError("Native transport is closed; create a new instance")
            if self._socket is not None:
                return
            with anyio.CancelScope() as scope:
                self._connecting_scope = scope
                try:
                    with anyio.fail_after(self.connect_timeout):
                        while True:
                            try:
                                connection = await self._open_socket()
                            except OSError as error:
                                if error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                                    raise
                                await anyio.sleep(0.05)
                            else:
                                if self._closed:
                                    anyio.notify_closing(connection)
                                    connection.close()
                                    break
                                self._socket = connection
                                break
                finally:
                    self._connecting_scope = None
            if self._closed:
                raise RuntimeError("Native transport closed while connecting")

    def _connected(self) -> socket.socket:
        if self._closed or self._socket is None:
            raise ConnectionError("Native transport is not connected")
        return self._socket

    @staticmethod
    def _message(line: bytes) -> JSONObject:
        try:
            return json_loads_object(line.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as error:
            raise ValueError("Native Claude frame must be a UTF-8 JSON object") from error

    async def write(self, data: str) -> None:
        """Send complete NDJSON frames; concurrent calls never interleave bytes."""
        # Bound allocations before UTF-8 encoding, then check the actual wire size.
        if len(data) > self.max_message_bytes + 1 or not data.endswith("\n"):
            raise ValueError("Native Claude write must be bounded, newline-terminated JSON")
        payload = data.encode("utf-8")
        if len(payload) > self.max_message_bytes + 1:
            raise ValueError("Native Claude write exceeds max_message_bytes")
        for line in payload[:-1].split(b"\n"):
            _ = self._message(line)
        async with self._write_lock:
            connection = self._connected()
            try:
                remaining = memoryview(payload)
                while remaining:
                    await checkpoint()
                    try:
                        sent = connection.send(remaining)
                    except BlockingIOError:
                        await anyio.wait_writable(connection)
                    else:
                        if not sent:
                            raise ConnectionError("Native Claude socket stopped accepting data")
                        remaining = remaining[sent:]
            except BaseException:
                # A cancelled send may have written half a frame; it cannot be reused.
                await self.close()
                raise

    async def read_messages(self) -> AsyncIterator[JSONObject]:
        """Yield complete objects, retaining at most one bounded partial frame."""
        connection = self._connected()
        if self._reading:
            raise RuntimeError("Native transport already has a message reader")
        self._reading = True
        pending = bytearray()
        try:
            while not self._closed:
                try:
                    await checkpoint()
                    try:
                        chunk = connection.recv(min(65536, self.max_message_bytes + 1 - len(pending)))
                    except BlockingIOError:
                        await anyio.wait_readable(connection)
                        continue
                except (anyio.ClosedResourceError, OSError) as error:
                    if self._closed:
                        return
                    raise ConnectionError("Native Claude socket failed") from error
                if not chunk:
                    if self._closed:
                        return
                    if pending:
                        raise EOFError("Native Claude socket closed during a JSON frame")
                    raise ConnectionError("Native Claude socket disconnected")
                pending.extend(chunk)
                while (newline := pending.find(b"\n")) >= 0:
                    line = bytes(pending[:newline])
                    del pending[:newline + 1]
                    if not line.strip():
                        continue
                    message = self._message(line)
                    if message.get("type") == "tasksolver_error":
                        detail = message.get("error")
                        if not isinstance(detail, str):
                            raise ValueError("Malformed native Claude bridge error")
                        raise RuntimeError(detail)
                    yield message
                    if self._closed:
                        return
                if len(pending) > self.max_message_bytes:
                    raise ValueError("Native Claude frame exceeds max_message_bytes")
        finally:
            self._reading = False
            await self.close()

    async def end_input(self) -> None:
        """Keep the persistent connection open after an SDK prompt iterable ends."""

    async def close(self) -> None:
        """Release this socket only; the native UI and any running turn continue."""
        self._closed = True
        if self._connecting_scope is not None:
            self._connecting_scope.cancel()
        connection, self._socket = self._socket, None
        if connection is not None:
            # Wake readiness waiters before closing their descriptor. There is no
            # unbounded await, even inside the SDK's shielded disconnect cleanup.
            anyio.notify_closing(connection)
            connection.close()
