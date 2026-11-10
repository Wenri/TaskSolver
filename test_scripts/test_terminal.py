"""Offline integration checks for the private tmux screen manager."""
from __future__ import annotations

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wirecap.runtime.terminal import TerminalManager, TerminalScreen, _process_start


def wait_for(check: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError("terminal condition did not become true")
        time.sleep(0.02)


def python_command(code: str, *args: str) -> list[str]:
    return [sys.executable, "-u", "-c", code, *args]


_ECHO = """
import json, sys
print('READY', flush=True)
for line in sys.stdin:
    print('GOT:' + json.dumps(line.rstrip('\\n'), ensure_ascii=False), flush=True)
"""


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for terminal integration checks")
class TerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tasksolver-terminal-test-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.manager = TerminalManager(workspace=self.workspace, width=100, height=20)
        self.addCleanup(self.manager.close)

    def echo(self, name: str) -> TerminalScreen:
        screen = self.manager.open(name, python_command(_ECHO))
        wait_for(lambda: "READY" in screen.snapshot())
        return screen

    def test_private_server_has_no_bootstrap_screen(self) -> None:
        self.assertEqual(dict(self.manager.screens), {})
        self.assertTrue(self.manager.socket_path.exists())
        self.assertEqual(self.manager._run(["list-sessions"]).stdout, b"")
        first = self.echo("first")
        first.close()
        self.assertEqual(dict(self.manager.screens), {})
        self.assertTrue(self.manager.socket_path.exists())
        self.assertTrue(self.echo("replacement").alive)

    def test_independent_input_unicode_and_prompt_privacy(self) -> None:
        first, second = self.echo("first"), self.echo("second")
        prompt = "héllo 世界 'quoted' ; $(touch not-created) & | " + "\\"
        calls: list[tuple[list[str], bytes | None]] = []
        run = self.manager._run

        def record(args: Sequence[str], *, data: bytes | None = None,
                   check: bool = True) -> subprocess.CompletedProcess[bytes]:
            calls.append((list(args), data))
            return run(args, data=data, check=check)

        with patch.object(self.manager, "_run", record):
            first.submit(prompt)
        expected = "GOT:" + json.dumps(prompt, ensure_ascii=False)
        wait_for(lambda: expected in first.snapshot())
        self.assertNotIn(expected, second.snapshot())
        self.assertFalse((self.workspace / "not-created").exists())
        self.assertTrue(any(data == prompt.encode() for _, data in calls))
        self.assertFalse(any(prompt in arg for args, _ in calls for arg in args))
        self.assertEqual(self.manager._run(["list-buffers"]).stdout, b"")
        second.send("not submitted")
        self.assertNotIn("GOT:", second.snapshot())
        second.send_keys("Enter")
        wait_for(lambda: 'GOT:"not submitted"' in second.snapshot())

    def test_multiline_paste_preserves_line_breaks(self) -> None:
        code = """
import json, os, tty
tty.setraw(0)
os.write(1, b'READY')
data = bytearray()
while len(data) < int(os.environ['EXPECTED_BYTES']):
    data.extend(os.read(0, 4096))
os.write(1, ('RECEIVED:' + json.dumps(data.decode(), ensure_ascii=False)).encode())
"""
        value = "α\nβ\nquoted $() ;\tESC\x1bend"
        env = dict(os.environ, EXPECTED_BYTES=str(len(value.encode())))
        with TerminalManager(workspace=self.workspace, env=env) as manager:
            screen = manager.open("raw", python_command(code))
            wait_for(lambda: "READY" in screen.snapshot())
            screen.send(value)
            wait_for(lambda: not screen.alive)
            self.assertIn("RECEIVED:" + json.dumps(value, ensure_ascii=False), screen.snapshot())

    def test_literal_command_arguments_and_single_executable_path(self) -> None:
        executable = self.workspace / "program with spaces; $(not-a-command)"
        executable.write_text(f"#!{sys.executable}\nprint('SINGLE EXECUTABLE')\n")
        executable.chmod(0o700)
        screen = self.manager.open("literal ; $name", [str(executable)])
        wait_for(lambda: not screen.alive)
        self.assertEqual(screen.exit_status, 0)
        self.assertIn("SINGLE EXECUTABLE", screen.snapshot())
        values = ["a b", ";", "$(touch should-not-exist)", "'quote'", "世界"]
        args = self.manager.open("arguments", python_command(
            "import json,sys; print(json.dumps(sys.argv[1:],ensure_ascii=False))", *values))
        wait_for(lambda: not args.alive)
        self.assertIn(json.dumps(values, ensure_ascii=False), args.snapshot())
        self.assertFalse((self.workspace / "should-not-exist").exists())

    def test_semicolon_names_and_keys_remain_literal(self) -> None:
        for name in (";", "ends;", "\\;"):
            screen = self.echo(name)
            actual = self.manager._run(["display-message", "-p", "-t", screen.target,
                                        "#{window_name}"]).stdout.decode().strip()
            # tmux renders backslashes in window names using its escaped display form.
            self.assertEqual(actual, name.replace("\\", "\\\\"))
            screen.send_keys(";", "Enter")
            wait_for(lambda: 'GOT:";"' in screen.snapshot())
        workdir = self.workspace / "cwd\\;"
        workdir.mkdir()
        screen = self.manager.open("cwd", python_command("import os; print(os.getcwd())"), cwd=workdir)
        wait_for(lambda: not screen.alive)
        self.assertIn(str(workdir), screen.snapshot())

    def test_exit_retains_complete_tail_and_history(self) -> None:
        code = "for n in range(1200): print(f'line-{n:04d}');\nraise SystemExit(7)"
        screen = self.manager.open("finished", python_command(code))
        wait_for(lambda: not screen.alive)
        self.assertEqual(screen.exit_status, 7)
        lines = [line for line in screen.snapshot(2000).splitlines() if line.startswith("line-")]
        self.assertEqual(lines, [f"line-{n:04d}" for n in range(1200)])
        self.assertIn("line-1199", screen.snapshot())
        self.assertIn("finished", self.manager.screens)
        with self.assertRaisesRegex(RuntimeError, "exited"):
            screen.submit("too late")

    def test_wait_closes_exited_screens_and_ends_with_the_last_one_or_the_server(self) -> None:
        import threading

        first = self.manager.open("first", python_command("import sys; sys.stdin.readline()"))
        second = self.echo("second")
        waiter = threading.Thread(target=lambda: self.manager.wait(close_exited=True, interval=0.02))
        waiter.start()
        self.addCleanup(waiter.join)
        first.submit("done")
        wait_for(lambda: list(self.manager.screens) == ["second"])
        self.assertEqual(first.exit_status, 0)
        self.assertTrue(waiter.is_alive())
        # A window removed inside tmux (Ctrl-b &) counts as an exited screen.
        self.manager._run(["kill-window", "-t", second.window_id])
        waiter.join(timeout=5)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(dict(self.manager.screens), {})

        third = self.echo("third")
        self.assertRaises(ValueError, self.manager.wait, interval=0)
        waiter = threading.Thread(target=self.manager.wait)
        waiter.start()
        self.addCleanup(waiter.join)
        self.manager._run(["kill-server"], check=False)
        waiter.join(timeout=5)
        self.assertFalse(waiter.is_alive())
        self.assertIn("third", self.manager.screens)  # retained without close_exited
        del third

    def test_validation_and_current_terminal_only_attach(self) -> None:
        screen = self.echo("same")
        with self.assertRaises(ValueError):
            self.manager.open("same", python_command("pass"))
        with self.assertRaises(ValueError):
            self.manager.open("", python_command("pass"))
        with self.assertRaises(ValueError):
            self.manager.open("invalid\nname", python_command("pass"))
        with self.assertRaises(ValueError):
            self.manager.open("empty", [])
        with self.assertRaises(ValueError):
            screen.snapshot(-1)
        argv = self.manager.attach_argv("same", read_only=True)
        self.assertIn("-r", argv)
        self.assertIn("-E", argv)
        self.assertEqual(argv[argv.index("-S") + 1], str(self.manager.socket_path))
        with patch("os.isatty", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "stdin and stdout"):
                self.manager.attach()
        screen.close()
        screen.close()
        self.assertFalse(screen.alive)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            screen.snapshot()

    def test_view_selection_is_isolated_and_cleanup_keeps_backend(self) -> None:
        first, second = self.echo("first"), self.echo("second")
        self.manager.attach_argv("first")
        argv, cleanup = self.manager._create_view("second", read_only=True)
        self.addCleanup(cleanup)
        view = argv[-1]

        def current(session: str) -> str:
            return self.manager._run(["display-message", "-p", "-t", session,
                                      "#{window_id}"]).stdout.decode().strip()

        self.assertEqual(current("tasksolver"), first.window_id)
        self.assertEqual(current(view), second.window_id)
        windows = self.manager._run(["list-windows", "-t", view, "-F", "#{window_id}"])
        self.assertEqual(windows.stdout.decode().splitlines(), [second.window_id])
        missing = self.manager._run(["select-window", "-t", f"{view}:{first.window_id}"], check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.manager.attach_argv("second")
        self.assertEqual(current(view), second.window_id)
        self.assertEqual(current("tasksolver"), second.window_id)
        cleanup()
        cleanup()
        self.assertTrue(first.alive and second.alive)

    def test_interrupted_attach_restores_current_terminal(self) -> None:
        self.manager.open("attach", python_command("import time; print('ATTACH_READY'); time.sleep(60)"))
        argv = self.manager.attach_argv()
        result = self.workspace / "attach-result"
        pid, master = pty.fork()
        if pid == 0:
            try:
                self.manager._env["TERM"] = "xterm-256color"
                before = termios.tcgetattr(0)
                # The server belongs to the parent, so use its already validated argv after fork.
                with patch.object(self.manager, "attach_argv", return_value=argv):
                    try:
                        self.manager.attach()
                    except KeyboardInterrupt:
                        pass
                    else:
                        raise AssertionError("attach did not propagate interruption")
                if termios.tcgetattr(0) != before:
                    raise AssertionError("attach left terminal attributes changed")
                result.write_text("restored")
            except BaseException as error:
                result.write_text(repr(error))
                os._exit(1)
            os._exit(0)
        reaped = False
        try:
            deadline, seen = time.monotonic() + 5, b""
            while b"ATTACH_READY" not in seen and time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    seen += os.read(master, 65536)
            self.assertIn(b"ATTACH_READY", seen)
            os.kill(pid, signal.SIGINT)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ended, status = os.waitpid(pid, os.WNOHANG)
                if ended:
                    reaped = True
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0, result.read_text())
                    break
                if select.select([master], [], [], 0.02)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        pass
            self.assertTrue(reaped, "attach client did not exit after interruption")
            self.assertEqual(result.read_text(), "restored")
            self.assertTrue(self.manager.screens["attach"].alive)
        finally:
            if not reaped:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(master)

    def test_readonly_viewer_does_not_block_programmatic_input(self) -> None:
        screen = self.echo("observed")
        argv, cleanup = self.manager._create_view(read_only=True)
        pid, master = pty.fork()
        if pid == 0:
            os.execve(argv[0], argv, dict(os.environ, TERM="xterm-256color"))
        try:
            wait_for(lambda: bool(self.manager._run(["list-clients"]).stdout))
            screen.submit("programmatic")
            wait_for(lambda: 'GOT:"programmatic"' in screen.snapshot())
            screen.send("separate Enter")
            screen.send_keys("Enter")
            wait_for(lambda: 'GOT:"separate Enter"' in screen.snapshot())
            os.write(master, b"viewer input\r")
            time.sleep(0.05)
            self.assertNotIn('GOT:"viewer input"', screen.snapshot())
        finally:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
            os.close(master)
            cleanup()

    def test_close_kills_ignored_hup_and_separate_process_groups(self) -> None:
        child = """
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
while True: time.sleep(1)
"""
        code = """
import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
for path, separate in zip(sys.argv[2:4], (False, True)):
    subprocess.Popen([sys.executable, '-c', sys.argv[4], path], start_new_session=separate)
while True: time.sleep(1)
"""
        paths = [self.workspace / name for name in ("leader", "child", "detached")]
        self.manager.open("stubborn", python_command(code, *(str(p) for p in paths), child))
        wait_for(lambda: all(path.exists() for path in paths))
        pids = [int(path.read_text()) for path in paths]
        assert self.manager._server is not None
        server_pid = self.manager._server.pid
        directory = self.manager.directory
        with TerminalManager(workspace=self.workspace) as unrelated:
            other = unrelated.open("unrelated", python_command(_ECHO))
            started = time.monotonic()
            self.manager.close()
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(other.alive)
        self.assertTrue(all(_process_start(pid) is None for pid in [server_pid, *pids]))
        self.assertFalse(directory.exists())
        self.manager.close()

    def test_exited_leader_still_owns_detached_descendants(self) -> None:
        pid_path = self.workspace / "orphan"
        child = ("import os,signal,sys,time; from pathlib import Path; "
                 "signal.signal(signal.SIGHUP,signal.SIG_IGN); "
                 "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                 "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
        code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]],start_new_session=True)"
        screen = self.manager.open("orphan", python_command(code, child, str(pid_path)))
        wait_for(lambda: pid_path.exists() and not screen.alive)
        pid = int(pid_path.read_text())
        self.assertIsNotNone(_process_start(pid))
        screen.close()
        self.assertIsNone(_process_start(pid))

    def test_startup_failure_unwinds_server_and_directory(self) -> None:
        processes: list[int] = []
        directories: list[Path] = []

        def fail(manager: TerminalManager, args: Sequence[str], *, data: bytes | None = None,
                 check: bool = True) -> subprocess.CompletedProcess[bytes]:
            assert manager._server is not None
            processes.append(manager._server.pid)
            directories.append(manager.directory)
            raise RuntimeError("configuration fixture")

        with patch.object(TerminalManager, "_run", fail):
            with self.assertRaisesRegex(RuntimeError, "configuration fixture"):
                TerminalManager(workspace=self.workspace)
        self.assertTrue(processes)
        self.assertTrue(all(_process_start(pid) is None for pid in processes))
        self.assertTrue(all(not path.exists() for path in directories))

    def test_open_failure_unwinds_only_new_window(self) -> None:
        first = self.echo("existing")
        run = self.manager._run

        def fail(args: Sequence[str], *, data: bytes | None = None,
                 check: bool = True) -> subprocess.CompletedProcess[bytes]:
            result = run(args, data=data, check=check)
            if args[0] == "new-window":
                raise RuntimeError("create fixture")
            return result

        with patch.object(self.manager, "_run", fail):
            with self.assertRaisesRegex(RuntimeError, "create fixture"):
                self.manager.open("failure", python_command(_ECHO))
        self.assertEqual(list(self.manager.screens), ["existing"])
        self.assertTrue(first.alive)
        windows = run(["list-windows", "-t", "tasksolver", "-F", "#{window_id}"]).stdout.decode().splitlines()
        self.assertEqual(windows, [first.window_id])

    def test_cleanup_callback_failure_does_not_skip_teardown(self) -> None:
        self.echo("alive")
        calls: list[str] = []

        def fail() -> None:
            calls.append("failed")
            raise ValueError("cleanup fixture")

        self.manager._register_cleanup(lambda: calls.append("finished"))
        self.manager._register_cleanup(fail)
        directory = self.manager.directory
        assert self.manager._server is not None
        server = self.manager._server
        with self.assertRaisesRegex(ValueError, "cleanup fixture"):
            self.manager.close()
        self.assertEqual(calls, ["failed", "finished"])
        self.assertFalse(directory.exists())
        self.assertIsNotNone(server.poll())
        self.manager.close()

    def test_last_screen_closes_viewers_and_manager_can_reopen(self) -> None:
        first, last = self.echo("first"), self.echo("last")
        calls: list[str] = []

        def fail() -> None:
            calls.append("failed")
            raise ValueError("viewer cleanup fixture")

        self.manager._register_cleanup(lambda: calls.append("finished"))
        self.manager._register_cleanup(fail)
        first.close()
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(ValueError, "viewer cleanup fixture"):
            last.close()
        self.assertEqual(calls, ["failed", "finished"])
        self.assertEqual(dict(self.manager.screens), {})
        self.assertTrue(self.echo("reopened").alive)
        self.manager.close()
        self.assertEqual(calls, ["failed", "finished"])


class ConstructorTests(unittest.TestCase):
    def test_missing_tmux_and_invalid_dimensions(self) -> None:
        with self.assertRaises(FileNotFoundError):
            TerminalManager(tmux_bin="/no/such/tasksolver-tmux")
        for width, height in ((0, 20), (80, -1)):
            with self.subTest(width=width, height=height), self.assertRaises(ValueError):
                TerminalManager(width=width, height=height)


if __name__ == "__main__":
    unittest.main()
