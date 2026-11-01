"""Offline checks for independent web views and abrupt viewer termination."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from websockets.sync.client import connect
from websockets.typing import Origin, Subprotocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_webterm import browser, receive
from tasksolver import terminal as frontend
from wirecap.runtime.terminal import TerminalManager, TerminalScreen, _process_start
from wirecap.runtime.webterm import WebTerminal


class WebTerminalViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("tmux") or not shutil.which("ttyd"):
            raise RuntimeError("these native web-terminal checks require tmux and ttyd on PATH")

    def setUp(self) -> None:
        self.manager = TerminalManager()
        self.addCleanup(self.manager.close)

    def screen(self, name: str) -> TerminalScreen:
        return self.manager.open(name, [
            sys.executable, "-u", "-c",
            "import sys\nprint(sys.argv[1], flush=True)\n"
            "for line in sys.stdin:\n print('REPLY:' + line.strip(), flush=True)\n",
            name,
        ])

    def active_window(self, session: str = "tasksolver") -> str:
        return self.manager._run([
            "display-message", "-p", "-t", f"{session}:", "#{window_id}",
        ]).stdout.decode().strip()

    def clients(self) -> dict[int, str]:
        result: dict[int, str] = {}
        for line in self.manager._run([
            "list-clients", "-F", "#{client_pid}\t#{session_name}",
        ]).stdout.decode().splitlines():
            pid, session = line.split("\t")
            result[int(pid)] = session
        return result

    def wait_until(self, predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("viewer cleanup did not finish")

    def test_grouped_views_keep_independent_selected_windows(self) -> None:
        first = self.screen("FIRST_SCREEN")
        second = self.screen("SECOND_SCREEN")
        self.assertEqual(self.active_window(), first.window_id)
        with WebTerminal(self.manager, port=0, screen=second.name) as second_view:
            with browser(second_view) as second_browser:
                receive(second_browser, b"SECOND_SCREEN")
                self.assertEqual(self.active_window(), first.window_id)
                second_sessions = set(self.clients().values())
                self.assertEqual(len(second_sessions), 1)
                second_session = second_sessions.pop()
                self.assertNotEqual(second_session, "tasksolver")
                self.assertEqual(self.active_window(second_session), second.window_id)
                with WebTerminal(self.manager, port=0, screen=first.name) as first_view:
                    with browser(first_view) as first_browser:
                        receive(first_browser, b"FIRST_SCREEN")
                        first_sessions = set(self.clients().values()) - {second_session}
                        self.assertEqual(len(first_sessions), 1)
                        first_session = first_sessions.pop()
                        self.assertEqual(self.active_window(first_session), first.window_id)
                        # Selecting a local screen must not move either existing viewer.
                        self.manager.attach_argv(screen=second.name)
                        self.assertEqual(self.active_window(), second.window_id)
                        self.assertEqual(self.active_window(first_session), first.window_id)
                        self.assertEqual(self.active_window(second_session), second.window_id)
                        first.submit("first-only")
                        receive(first_browser, b"REPLY:first-only")
                        second.submit("second-only")
                        receive(second_browser, b"REPLY:second-only")
                self.assertTrue(first.alive)
                self.assertTrue(second.alive)
                self.wait_until(lambda: set(self.clients().values()) == {second_session})
                self.assertEqual(set(self.clients().values()), {second_session})
        self.assertTrue(first.alive)
        self.assertTrue(second.alive)
        self.wait_until(lambda: not self.clients())
        self.assertEqual(self.clients(), {})

    def test_close_after_forced_ttyd_exit_removes_only_its_clients(self) -> None:
        screen = self.screen("SURVIVING_SCREEN")
        with WebTerminal(self.manager, port=0) as view:
            with browser(view) as connection:
                receive(connection, b"SURVIVING_SCREEN")
                clients = self.clients()
                self.assertEqual(len(clients), 1)
                identities = {pid: _process_start(pid) for pid in clients}
                self.assertTrue(all(identity is not None for identity in identities.values()))
                process = view._process
                assert process is not None
                process.kill()
                process.wait(timeout=3)
                view.close()
                self.wait_until(lambda: not self.clients())
                self.wait_until(lambda: all(
                    _process_start(pid) != identity for pid, identity in identities.items()))
                sessions = self.manager._run([
                    "list-sessions", "-F", "#{session_name}",
                ]).stdout.decode().splitlines()
                self.assertEqual(sessions, ["tasksolver"])
                self.assertTrue(screen.alive)
                screen.submit("still-running")
                self.wait_until(lambda: "REPLY:still-running" in screen.snapshot())

    def test_last_screen_closes_old_view_and_manager_can_open_another(self) -> None:
        original = self.screen("ORIGINAL_SCREEN")
        with WebTerminal(self.manager, port=0) as original_view:
            with browser(original_view) as original_browser:
                receive(original_browser, b"ORIGINAL_SCREEN")
                identities = {pid: _process_start(pid) for pid in self.clients()}
                original.close()
                self.assertFalse(original_view.alive)
                self.assertEqual(dict(self.manager.screens), {})
                self.wait_until(lambda: all(
                    _process_start(pid) != identity for pid, identity in identities.items()))
            replacement = self.screen("REPLACEMENT_SCREEN")
            with WebTerminal(self.manager, port=0) as replacement_view:
                with browser(replacement_view) as replacement_browser:
                    receive(replacement_browser, b"REPLACEMENT_SCREEN")
                    original_view.close()
                    self.assertTrue(replacement_view.alive)
                    replacement.submit("new-session")
                    receive(replacement_browser, b"REPLY:new-session")
            self.assertTrue(replacement.alive)

    def test_cli_local_and_web_share_one_process_until_keyboard_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "screen-state"

            def open_echo(_backend: frontend.Backend, *, manager: TerminalManager,
                          model: str | None = None, capture: bool | str = True) -> TerminalScreen:
                del model, capture
                screen = manager.open("echo", [
                    sys.executable, "-u", "-c",
                    "import os,sys\npid = str(os.getpid())\n"
                    "print('AGENT_READY:' + pid, flush=True)\n"
                    "for line in sys.stdin:\n"
                    " print('AGENT_REPLY:' + pid + ':' + line.strip(), flush=True)\n",
                ])
                pane_pid = manager._run([
                    "display-message", "-p", "-t", screen.pane_id, "#{pane_pid}",
                ]).stdout.decode().strip()
                state.write_text(f"{manager.directory}\n{pane_pid}\n")
                return screen

            pid, master = pty.fork()
            if pid == 0:
                try:
                    os.environ["TERM"] = "xterm-256color"
                    with patch.object(frontend, "open_terminal", open_echo):
                        status = frontend.main(["agy", "--web-port", "0", "--writable", "--no-capture"])
                except BaseException as error:
                    print(repr(error), file=sys.stderr, flush=True)
                    os._exit(1)
                os._exit(status)

            output = bytearray()
            reaped = False
            exit_status: int | None = None
            owned: dict[int, int] = {}

            def collect_processes() -> None:
                pending = [pid]
                while pending:
                    parent = pending.pop()
                    try:
                        children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
                    except OSError:
                        continue
                    for child in map(int, children):
                        identity = _process_start(child)
                        if identity is not None and child not in owned:
                            owned[child] = identity
                            pending.append(child)

            def pump_terminal() -> None:
                if select.select([master], [], [], 0.02)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        pass

            def terminal_contains(needle: bytes) -> None:
                deadline = time.monotonic() + 8
                while needle not in output and time.monotonic() < deadline:
                    pump_terminal()
                self.assertIn(needle, output)

            def reap(timeout: float) -> None:
                nonlocal reaped, exit_status
                deadline = time.monotonic() + timeout
                while not reaped and time.monotonic() < deadline:
                    ended, status = os.waitpid(pid, os.WNOHANG)
                    if ended:
                        reaped = True
                        exit_status = os.waitstatus_to_exitcode(status)
                    else:
                        pump_terminal()

            try:
                terminal_contains(b"AGENT_READY:")
                manager_path, agent_pid = state.read_text().splitlines()
                ready = f"AGENT_READY:{agent_pid}".encode()
                terminal_contains(ready)
                url_match = re.search(rb"http://127\.0\.0\.1:\d+", output)
                assert url_match is not None, output
                url = url_match[0].decode()
                with connect(url.replace("http:", "ws:") + "/ws", origin=Origin(url),
                             subprotocols=[Subprotocol("tty")], close_timeout=1) as connection:
                    connection.send(json.dumps({"AuthToken": "", "columns": 100, "rows": 30}))
                    receive(connection, ready)
                    collect_processes()
                    self.assertGreaterEqual(len(owned), 5)  # server, ttyd, two clients, one echo process
                    for source, payload in (("local", b"from-local"), ("web", b"from-web")):
                        if source == "local":
                            os.write(master, payload + b"\r")
                        else:
                            connection.send(b"0" + payload + b"\r")
                        reply = f"AGENT_REPLY:{agent_pid}:".encode() + payload
                        receive(connection, reply)
                        terminal_contains(reply)
                    os.write(master, b"\x02d")
                    terminal_contains(b"Press Ctrl-C to close viewers and screens.")
                    connection.send(b"0after-detach\r")
                    receive(connection, f"AGENT_REPLY:{agent_pid}:after-detach".encode())
                    os.write(master, b"\x03")
                    reap(8)
                self.assertTrue(reaped, output.decode("utf-8", "replace"))
                self.assertEqual(exit_status, 0, output.decode("utf-8", "replace"))
                self.assertFalse(Path(manager_path).exists())
                self.wait_until(lambda: all(
                    _process_start(child) != identity for child, identity in owned.items()))
            finally:
                if not reaped:
                    collect_processes()
                    os.kill(pid, signal.SIGINT)
                    reap(3)
                if not reaped:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                for child, identity in owned.items():
                    if _process_start(child) == identity:
                        try:
                            os.kill(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                os.close(master)


if __name__ == "__main__":
    unittest.main(verbosity=2)
