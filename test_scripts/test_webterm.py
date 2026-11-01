"""Offline HTTP/WebSocket terminal tests; requires tmux and ttyd on PATH."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time
import unittest
from typing import cast
from unittest.mock import patch

from websockets.exceptions import InvalidHandshake
from websockets.sync.client import ClientConnection, connect
from websockets.sync.connection import Connection
from websockets.typing import Origin, Subprotocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wirecap.runtime.terminal import TerminalManager, TerminalScreen
from wirecap.runtime.webterm import WebTerminal


def receive(connection: Connection, needle: bytes, timeout: float = 5.0) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = connection.recv(timeout=max(0.01, deadline - time.monotonic()))
        data = message.encode() if isinstance(message, str) else message
        if data.startswith(b"0"):
            output.extend(data[1:])
        if needle in output:
            return bytes(output)
    raise AssertionError(f"web terminal did not display {needle!r}: {bytes(output)!r}")


def browser(view: WebTerminal) -> ClientConnection:
    connection = connect(view.url.replace("http:", "ws:") + "/ws",
                         origin=Origin(view.url), subprotocols=[Subprotocol("tty")], close_timeout=1)
    connection.send(json.dumps({"AuthToken": "", "columns": 100, "rows": 30}))
    return connection


class WebTerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("tmux") or not shutil.which("ttyd"):
            raise RuntimeError("these native web-terminal checks require tmux and ttyd on PATH")

    def setUp(self) -> None:
        self.manager = TerminalManager()
        self.addCleanup(self.manager.close)

    def echo_screen(self, name: str = "echo") -> TerminalScreen:
        code = "import sys\nprint('WEB_READY', flush=True)\nfor line in sys.stdin:\n print('REPLY:' + line.strip(), flush=True)\n"
        return self.manager.open(name, [sys.executable, "-u", "-c", code])

    def test_readonly_web_view_retains_live_terminal(self) -> None:
        screen = self.echo_screen()
        view = WebTerminal(self.manager, port=0)
        self.addCleanup(view.close)
        http_connection = http.client.HTTPConnection("127.0.0.1", view.port, timeout=3)
        try:
            http_connection.request("GET", "/")
            response = http_connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"ttyd", response.read())
        finally:
            http_connection.close()
        with browser(view) as connection:
            receive(connection, b"WEB_READY")
            connection.send(b"0rejected\r")
            screen.submit("accepted")
            output = receive(connection, b"REPLY:accepted")
            self.assertNotIn(b"rejected", output)
            self.assertNotIn("rejected", screen.snapshot())
        view.close()
        view.close()
        self.assertFalse(view.alive)
        self.assertTrue(screen.alive)

    def test_writable_browser_and_manager_shutdown(self) -> None:
        screen = self.echo_screen()
        view = WebTerminal(self.manager, port=0, writable=True)
        with browser(view) as connection:
            receive(connection, b"WEB_READY")
            connection.send(b"0from-browser\r")
            receive(connection, b"REPLY:from-browser")
            self.manager.close()
        self.assertFalse(view.alive)
        self.assertFalse(screen.alive)

    def test_websocket_origin_is_checked(self) -> None:
        self.echo_screen()
        with WebTerminal(self.manager, port=0) as view:
            with self.assertRaises((InvalidHandshake, EOFError)):
                with connect(view.url.replace("http:", "ws:") + "/ws",
                             origin=Origin("https://unrelated.example"),
                             subprotocols=[Subprotocol("tty")]):
                    pass

    def test_port_conflict_does_not_accept_an_unrelated_listener(self) -> None:
        self.echo_screen()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            address = cast(tuple[str, int], occupied.getsockname())
            with self.assertRaisesRegex(RuntimeError, "ttyd failed"):
                WebTerminal(self.manager, port=address[1], timeout=2)
        self.assertTrue(self.manager.screens["echo"].alive)

    def test_failed_start_is_reaped(self) -> None:
        self.echo_screen()
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "ttyd-stall"
            pid = Path(directory) / "pid"
            executable.write_text(
                f"#!{sys.executable}\nimport os, signal, sys, time\n"
                "if '--help' in sys.argv:\n print('--writable'); sys.exit(0)\n"
                f"open({str(pid)!r}, 'w').write(str(os.getpid()))\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n")
            executable.chmod(0o700)
            with self.assertRaises(TimeoutError):
                WebTerminal(self.manager, ttyd_bin=str(executable), timeout=0.3)
            child = int(pid.read_text())
            with self.assertRaises(ChildProcessError):
                os.waitpid(child, os.WNOHANG)

    def test_older_writable_by_default_ttyd_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "ttyd-old"
            executable.write_text(f"#!{sys.executable}\nprint('--readonly')\n")
            executable.chmod(0o700)
            with self.assertRaisesRegex(RuntimeError, "--writable support"):
                WebTerminal(self.manager, ttyd_bin=str(executable))

    def test_missing_binary_and_invalid_options(self) -> None:
        with patch("wirecap.runtime.webterm.shutil.which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "ttyd"):
                WebTerminal(self.manager)
        for port in (-1, 65536):
            with self.assertRaises(ValueError):
                WebTerminal(self.manager, port=port)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                WebTerminal(self.manager, timeout=timeout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
