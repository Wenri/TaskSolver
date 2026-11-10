#!/usr/bin/env python3
"""Offline terminal frontend checks; no tmux, native CLI, or provider calls."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import NoReturn, Self
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for source in (ROOT, ROOT / "antigravity", ROOT / "claude", ROOT / "codex", ROOT / "kimi"):
    sys.path.insert(0, os.fspath(source))

from pycodex._env import NO_UPDATE_CHECK
from tasksolver import terminal
from wirecap.runtime.terminal import TerminalManager, TerminalScreen


@dataclass
class OpenCall:
    name: str
    command: tuple[str, ...]
    cwd: Path
    capture_path: Path | None


class RecordingManager(TerminalManager):
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.workspace = directory
        self.calls: list[OpenCall] = []
        self._screens: dict[str, TerminalScreen] = {}
        self._lock = threading.RLock()
        self.attached: list[tuple[str | None, bool]] = []
        self.on_attach: Callable[[], None] | None = None
        self.on_wait: Callable[[], None] | None = None
        self.waited: list[bool] = []
        self.ended = False
        self.closed = False
        self.fail_open = False

    @property
    def screens(self) -> Mapping[str, TerminalScreen]:
        return dict(self._screens)

    def open(self, name: str, command: Sequence[str], *,
             cwd: str | os.PathLike[str] | None = None,
             capture_path: Path | None = None) -> TerminalScreen:
        if self.fail_open:
            raise RuntimeError("screen creation failed")
        if name in self._screens:
            raise ValueError("duplicate screen")
        self.calls.append(OpenCall(name, tuple(command), Path(cwd or self.workspace), capture_path))
        screen = TerminalScreen(self, name, f"%{len(self.calls)}", f"@{len(self.calls)}",
                                capture_path=capture_path)
        self._screens[name] = screen
        return screen

    def attach(self, screen: str | None = None, read_only: bool = False, *,
               close_exited: bool = False) -> int:
        assert close_exited, "the launcher closes a screen when its agent exits"
        self.attached.append((screen, read_only))
        if self.on_attach is not None:
            self.on_attach()
        return 7

    def wait(self, *, close_exited: bool = False, interval: float = 0.2,
             timeout: float | None = None) -> bool:
        assert close_exited, "the launcher closes a screen when its agent exits"
        if timeout is None:
            # Blocks until the screens have ended.
            self.waited.append(close_exited)
            if self.on_wait is not None:
                self.on_wait()
            self.ended = True
        return self.ended

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self


class WebView:
    def __init__(self, manager: TerminalManager, *, screen: str | None = None, port: int = 7681,
                 writable: bool = False, ttyd_bin: str | None = None) -> None:
        self.manager = manager
        self.screen = screen
        self.port = port
        self.writable = writable
        self.ttyd_bin = ttyd_bin
        self.url = f"http://127.0.0.1:{port}"
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True


class TerminalFrontendTests(unittest.TestCase):
    def test_named_screens_have_unique_capture_and_no_credentials_in_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(Path(directory))
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "private-key"}):
                first = terminal.open_terminal("agy", manager=manager)
                second = terminal.open_terminal("agy", manager=manager)
                claude = terminal.open_terminal("claude", manager=manager)
            self.assertEqual([call.name for call in manager.calls], ["agy", "agy-2", "claude"])
            self.assertNotEqual(first.capture_path, second.capture_path)
            for screen in (first, second):
                self.assertIsNotNone(screen.capture_path)
                assert screen.capture_path is not None
                self.assertEqual(screen.capture_path.parent, manager.directory)
                self.assertTrue(screen.capture_path.is_file())
            self.assertIsNone(claude.capture_path)
            for call in manager.calls:
                self.assertEqual(call.command[:6],
                                 (sys.executable, "-E", "-P", "-m", "tasksolver.terminal", "--child"))
                self.assertNotIn("private-key", " ".join(call.command))
            self.assertNotIn("--capture-path", manager.calls[-1].command)

    def test_workspace_cannot_shadow_installed_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            marker = workspace / "shadow-executed"
            (workspace / "tasksolver.py").write_text(
                "from pathlib import Path\nPath('shadow-executed').touch()\nraise SystemExit(73)\n")
            manager = RecordingManager(workspace)
            terminal.open_terminal("claude", manager=manager)
            command = manager.calls[0].command
            result = subprocess.run([*command[:command.index("--child")], "--help"],
                                    cwd=workspace, capture_output=True, text=True, timeout=10)
            self.assertFalse(marker.exists())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Open the bundled agent UIs", result.stdout)

    def test_workspace_model_and_explicit_capture_are_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "space with spaces"
            workspace.mkdir()
            manager = RecordingManager(root)
            capture = root / "saved" / "wire.jsonl"
            screen = terminal.open_terminal("codex", manager=manager, name="review",
                                            workspace=workspace, model="test-model",
                                            capture=os.fspath(capture))
            call = manager.calls[0]
            self.assertEqual(screen.name, "review")
            self.assertEqual(call.cwd, workspace)
            self.assertEqual(call.capture_path, capture)
            self.assertEqual(call.command[-4:], ("--model", "test-model", "--capture-path", str(capture)))
            with self.assertRaises(ValueError):
                terminal.open_terminal("codex", manager=manager, name="other", capture=str(capture))

    def test_capture_opt_out_and_failed_open_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(Path(directory))
            screen = terminal.open_terminal("kimi", manager=manager, capture=False)
            self.assertIsNone(screen.capture_path)
            self.assertNotIn("--capture-path", manager.calls[0].command)
            manager.fail_open = True
            with self.assertRaisesRegex(RuntimeError, "screen creation failed"):
                terminal.open_terminal("codex", manager=manager)
            self.assertEqual(list(manager.directory.iterdir()), [])

    def test_native_child_environments_are_independent_and_interactive(self) -> None:
        inherited = {
            "WIRE_MP_BOOT_FD": "999", "WIRE_MODULE": "wrong.module", "WIRE_ENABLE": "0",
            "WIRE_CAPTURE": "/old/capture", "WIRE_NODE_ADDON": "/old/addon",
            "WIRE_MAXCOPY": "1", "AGY_PROC_REWRITE": "1", "AGY_PROC_LOG": "/old/log",
            "PYTHONHOME": "/other/python", "OPENAI_API_KEY": "private-key",
            "TASKSOLVER_TERMINAL_SCREEN": "owned-screen-token", "PATH": os.defpath,
        }
        backends: tuple[terminal.Backend, ...] = ("agy", "codex", "kimi")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, inherited, clear=True):
            workspace = Path(directory)
            capture = workspace / "capture.jsonl"
            for backend in backends:
                with self.subTest(backend=backend):
                    command, env = terminal._child_command(backend, workspace, "test-model", capture)
                    self.assertEqual(env["PYTHONHOME"], sys.prefix)
                    self.assertEqual(env["OPENAI_API_KEY"], "private-key")
                    self.assertEqual(env["TASKSOLVER_TERMINAL_SCREEN"], "owned-screen-token")
                    self.assertEqual(env["WIRE_CAPTURE"], str(capture))
                    self.assertEqual(env["WIRE_MODULE"], f"py{backend}.{backend}_process")
                    self.assertNotIn("WIRE_MP_BOOT_FD", env)
                    self.assertNotIn("AGY_PROC_REWRITE", env)
                    self.assertNotIn("AGY_PROC_LOG", env)
                    self.assertNotIn("private-key", " ".join(command))
                    for flag in ("--print", "--prompt-interactive", "--dangerously-skip-permissions",
                                 "--yolo", "--auto", "exec", "-p"):
                        self.assertNotIn(flag, command)
                    if backend == "agy":
                        self.assertEqual(command[1:], ["--model", "test-model"])
                    elif backend == "codex":
                        self.assertEqual(command[1:], ["-C", str(workspace), "--no-daemon",
                                                       *NO_UPDATE_CHECK, "-m", "test-model"])
                    else:
                        self.assertEqual(len(command), 2)
                        self.assertEqual(env["KIMI_MODEL_NAME"], "test-model")
                        self.assertEqual(env["WIRE_MAXCOPY"], "8388608")
            self.assertEqual(dict(os.environ), inherited)

    def test_disabling_capture_removes_native_enable_flags(self) -> None:
        backends: tuple[terminal.Backend, ...] = ("agy", "codex", "kimi")
        for backend in backends:
            with self.subTest(backend=backend):
                _, env = terminal._child_command(backend, ROOT, None, None)
                self.assertFalse(any(key.startswith(("WIRE_", "AGY_PROC_")) for key in env))
                self.assertEqual(env["PYTHONHOME"], sys.prefix)

    def test_claude_child_uses_native_ui_without_parent_sdk_flags(self) -> None:
        from pyclaude.client import PARENT_SESSION_ENV

        inherited = {
            **dict.fromkeys(PARENT_SESSION_ENV, "parent-value"),
            "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": "old", "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
            "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "true",
            "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1", "PYTHONHOME": "/other/python",
            "WIRE_CAPTURE": "/old/file", "AGY_PROC_ENABLE": "1",
            "ANTHROPIC_API_KEY": "private-key", "TASKSOLVER_TERMINAL_SCREEN": "screen-token",
        }
        with patch.dict(os.environ, inherited, clear=True), \
             patch("pyclaude._env.bundled_cli", return_value="/bundled/claude"):
            command, env = terminal._child_command("claude", ROOT, "sonnet", None)
        self.assertEqual(command, ["/bundled/claude", "--model", "sonnet"])
        self.assertEqual(env, {"ANTHROPIC_API_KEY": "private-key",
                               "TASKSOLVER_TERMINAL_SCREEN": "screen-token", "PWD": str(ROOT)})

    def test_claude_child_requires_the_bridged_bundle(self) -> None:
        with patch("pyclaude._env.bundled_cli", side_effect=FileNotFoundError("bundle missing")), \
             self.assertRaisesRegex(FileNotFoundError, "bundle missing"):
            terminal._child_command("claude", ROOT, None, None)

    def test_claude_screen_owns_its_sdk_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            socket = workspace / "sdk.sock"
            manager = RecordingManager(workspace)
            with patch.dict(os.environ, {"TASKSOLVER_CLAUDE_SOCKET": "/parent/socket"}), \
                 patch("pyclaude._env.bundled_cli", return_value="/bundled/claude"):
                screen = terminal.open_terminal("claude", manager=manager, sdk_socket=socket)
                command, env = terminal._child_command(
                    "claude", workspace, "sonnet", None, sdk_socket=socket)
                self.assertEqual(os.environ["TASKSOLVER_CLAUDE_SOCKET"], "/parent/socket")
                _, automatic_env = terminal._child_command("claude", workspace, None, None)
            self.assertEqual(screen.control_socket, socket)
            self.assertEqual(command, ["/bundled/claude", "--model", "sonnet"])
            self.assertEqual(env["TASKSOLVER_CLAUDE_SOCKET"], str(socket))
            self.assertNotIn("TASKSOLVER_CLAUDE_SOCKET", automatic_env)
            self.assertEqual(manager.calls[0].command[-2:], ("--sdk-socket", str(socket)))
            self.assertNotIn("--output-format", command)
            with self.assertRaisesRegex(ValueError, "already belongs to a screen"):
                terminal.open_terminal("claude", manager=manager, sdk_socket=socket)
            alias = workspace / "alias"
            alias.symlink_to(workspace, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "already belongs to a screen"):
                terminal.open_terminal("claude", manager=manager, sdk_socket=alias / socket.name)
            other = terminal.open_terminal("claude", manager=manager)
            third = terminal.open_terminal("claude", manager=manager)
            self.assertIsNotNone(other.control_socket)
            assert other.control_socket is not None
            self.assertEqual(other.control_socket.parent, workspace)
            self.assertNotEqual(other.control_socket, third.control_socket)

    def test_sdk_socket_configuration_fails_before_opening_a_screen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(Path(directory))
            socket = Path(directory) / "sdk.sock"
            with self.assertRaisesRegex(ValueError, "Claude backend"):
                terminal.open_terminal("codex", manager=manager, sdk_socket=socket)
            socket.symlink_to(Path(directory) / "absent")
            with self.assertRaises(FileExistsError):
                terminal.open_terminal("claude", manager=manager, sdk_socket=socket)
            self.assertEqual(manager.calls, [])

    def test_cli_attaches_existing_terminal_and_closes_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(Path(directory))
            configuration: list[tuple[str | None, str | None]] = []

            def factory(*, workspace: str | None = None, tmux_bin: str | None = None) -> RecordingManager:
                configuration.append((workspace, tmux_bin))
                return manager

            answers = [True, False]  # reattach once after the first detach, then the screens end

            def enter(_manager: TerminalManager) -> bool:
                return answers.pop(0)

            output = io.StringIO()
            with patch.object(terminal, "TerminalManager", new=factory), \
                 patch.object(terminal, "_enter", new=enter), redirect_stdout(output):
                result = terminal.main(["agy", "codex", "claude", "kimi", "--workspace", directory,
                                        "--tmux-bin", "/custom/tmux", "--no-web"])
            self.assertEqual(result, 0)
            self.assertEqual(configuration, [(directory, "/custom/tmux")])
            self.assertEqual([call.name for call in manager.calls], ["agy", "codex", "claude", "kimi"])
            # Without pages nothing is covered: attach at once, and offer it again after each detach.
            self.assertEqual(manager.attached, [(None, False), (None, False)])
            self.assertEqual(answers, [])
            self.assertEqual(output.getvalue().count("Press Enter to attach this terminal"), 2)
            self.assertTrue(manager.closed)
            for call in manager.calls:
                self.assertNotIn("--capture-path", call.command)

    def test_cli_capture_is_opt_in_and_names_one_file_per_captured_screen(self) -> None:
        cases = ((["codex", "claude"], ["run.jsonl", None]),
                 (["agy", "codex", "claude", "agy"], ["run-agy.jsonl", "run-codex.jsonl", None, "run-agy-2.jsonl"]))
        for backends, expected in cases:
            with self.subTest(backends=backends), tempfile.TemporaryDirectory() as directory:
                manager = RecordingManager(Path(directory))

                def factory(*, workspace: str | None = None, tmux_bin: str | None = None) -> RecordingManager:
                    return manager

                target = Path(directory).resolve() / "captures/run.jsonl"
                with patch.object(terminal, "TerminalManager", new=factory), \
                     patch.object(terminal, "_enter", return_value=False), redirect_stdout(io.StringIO()):
                    _ = terminal.main([*backends, "--no-web", "--capture", str(target)])
                paths = [call.command[call.command.index("--capture-path") + 1]
                         if "--capture-path" in call.command else None for call in manager.calls]
                self.assertEqual(paths, [None if name is None else str(target.with_name(name))
                                         for name in expected])

    def test_web_cli_is_headless_and_writable_by_default(self) -> None:
        for readonly, port in ((False, 7681), (True, 7681), (False, 0), (False, None)):
            writable = not readonly
            with self.subTest(readonly=readonly, port=port), tempfile.TemporaryDirectory() as directory:
                manager = RecordingManager(Path(directory))
                views: list[WebView] = []

                def manager_factory(*, workspace: str | None = None,
                                    tmux_bin: str | None = None) -> RecordingManager:
                    return manager

                def web_factory(owner: TerminalManager, *, screen: str | None = None, port: int = 7681,
                                writable: bool = False, ttyd_bin: str | None = None) -> WebView:
                    view = WebView(owner, screen=screen, port=port, writable=writable, ttyd_bin=ttyd_bin)
                    views.append(view)
                    return view

                def interrupt() -> NoReturn:
                    raise KeyboardInterrupt

                manager.on_wait = interrupt
                output = io.StringIO()
                args = ["codex", "codex", "--ttyd-bin", "/custom/ttyd"]
                if port is not None:
                    args += ["--web-port", str(port)]
                if readonly:
                    args.append("--readonly")
                with patch.object(terminal, "TerminalManager", new=manager_factory), \
                     patch("wirecap.runtime.webpage.WebScreen", new=web_factory), \
                     patch.object(os, "isatty", return_value=False), \
                     patch.object(sys, "stdin", io.StringIO()), redirect_stdout(output):
                    self.assertEqual(terminal.main(args), 0)
                self.assertEqual(manager.attached, [])
                self.assertEqual(manager.waited, [True])
                self.assertTrue(manager.closed)
                self.assertEqual([view.screen for view in views], ["codex", "codex-2"])
                # A page shows the live capture: without --capture it is the manager's temporary file.
                for call in manager.calls:
                    assert call.capture_path is not None
                    self.assertEqual(call.capture_path.parent, manager.directory)
                self.assertEqual([view.port for view in views], [port or 0, port + 1 if port else 0])
                for view in views:
                    self.assertEqual(view.writable, writable)
                    self.assertEqual(view.ttyd_bin, "/custom/ttyd")
                    self.assertTrue(view.closed)
                    self.assertIn(f"{view.screen}: {view.url}", output.getvalue())

    def test_web_cli_ends_when_the_screens_do(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(Path(directory))
            views: list[WebView] = []

            def manager_factory(*, workspace: str | None = None, tmux_bin: str | None = None) -> RecordingManager:
                return manager

            def web_factory(owner: TerminalManager, *, screen: str | None = None, port: int = 7681,
                            writable: bool = False, ttyd_bin: str | None = None) -> WebView:
                views.append(WebView(owner, screen=screen, port=port, writable=writable, ttyd_bin=ttyd_bin))
                return views[-1]

            with patch.object(terminal, "TerminalManager", new=manager_factory), \
                 patch("wirecap.runtime.webpage.WebScreen", new=web_factory), \
                 patch.object(os, "isatty", return_value=False), redirect_stdout(io.StringIO()):
                self.assertEqual(terminal.main(["codex"]), 0)
            self.assertEqual(manager.waited, [True])
            self.assertTrue(manager.closed)
            self.assertTrue(views[0].closed)

    def test_web_cli_attaches_a_complete_tty_on_request_and_again_after_detach(self) -> None:
        cases = ((True, True, False), (True, True, True),
                 (True, False, False), (False, True, False))
        for stdin_tty, stdout_tty, no_attach in cases:
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty, no_attach=no_attach), \
                 tempfile.TemporaryDirectory() as directory:
                manager = RecordingManager(Path(directory))
                views: list[WebView] = []
                output = io.StringIO()
                should_attach = stdin_tty and stdout_tty and not no_attach
                asked: list[int] = []

                def manager_factory(*, workspace: str | None = None,
                                    tmux_bin: str | None = None) -> RecordingManager:
                    return manager

                def web_factory(owner: TerminalManager, *, screen: str | None = None, port: int = 7681,
                                writable: bool = False, ttyd_bin: str | None = None) -> WebView:
                    view = WebView(owner, screen=screen, port=port, writable=writable, ttyd_bin=ttyd_bin)
                    views.append(view)
                    return view

                def enter(_manager: TerminalManager) -> bool:
                    # The URLs are on screen, and nothing is attached, each time the user is asked.
                    self.assertIn(f"codex: {views[0].url}", output.getvalue())
                    self.assertEqual(len(manager.attached), len(asked))
                    self.assertFalse(views[0].closed)
                    asked.append(output.getvalue().count("Press Enter to attach this terminal"))
                    return len(asked) < 3  # attach, detach, attach again, then Ctrl-C-less end

                def attached() -> None:
                    self.assertFalse(views[0].closed)
                    self.assertFalse(manager.closed)

                def is_tty(fd: int) -> bool:
                    return stdin_tty if fd == 0 else stdout_tty if fd == 1 else False

                def interrupt() -> NoReturn:
                    self.assertFalse(manager.closed)
                    self.assertFalse(views[0].closed)
                    self.assertIn("Press Ctrl-C to close the pages and screens.", output.getvalue())
                    raise KeyboardInterrupt

                manager.on_attach = attached
                manager.on_wait = interrupt
                args = ["codex", "--web-port", "7681"]
                if no_attach:
                    args.append("--no-attach")
                with patch.object(terminal, "TerminalManager", new=manager_factory), \
                     patch("wirecap.runtime.webpage.WebScreen", new=web_factory), \
                     patch.object(terminal, "_enter", new=enter), \
                     patch.object(os, "isatty", new=is_tty), redirect_stdout(output):
                    self.assertEqual(terminal.main(args), 0)
                self.assertEqual(manager.attached, [(None, False)] * 2 if should_attach else [])
                self.assertEqual(asked, [1, 2, 3] if should_attach else [])
                self.assertEqual(manager.waited, [] if should_attach else [True])
                self.assertTrue(manager.closed)
                self.assertTrue(views[0].closed)

    def test_interrupt_during_local_attach_cleans_up_with_or_without_web(self) -> None:
        for web in (False, True):
            with self.subTest(web=web), tempfile.TemporaryDirectory() as directory:
                manager = RecordingManager(Path(directory))
                views: list[WebView] = []

                def manager_factory(*, workspace: str | None = None,
                                    tmux_bin: str | None = None) -> RecordingManager:
                    return manager

                def web_factory(owner: TerminalManager, *, screen: str | None = None, port: int = 7681,
                                writable: bool = False, ttyd_bin: str | None = None) -> WebView:
                    view = WebView(owner, screen=screen, port=port, writable=writable, ttyd_bin=ttyd_bin)
                    views.append(view)
                    return view

                def interrupt() -> NoReturn:
                    raise KeyboardInterrupt

                def unexpected_wait() -> NoReturn:
                    raise AssertionError("Ctrl-C during attach must stop the viewer instead of waiting")

                manager.on_attach = interrupt
                manager.on_wait = unexpected_wait
                args = ["codex", *([] if web else ["--no-web"])]
                with patch.object(terminal, "TerminalManager", new=manager_factory), \
                     patch("wirecap.runtime.webpage.WebScreen", new=web_factory), \
                     patch.object(terminal, "_enter", return_value=True), \
                     patch.object(os, "isatty", return_value=True), redirect_stdout(io.StringIO()):
                    self.assertEqual(terminal.main(args), 0)
                self.assertEqual(manager.attached, [(None, False)])
                self.assertTrue(manager.closed)
                if web:
                    self.assertEqual(len(views), 1)
                    self.assertTrue(views[0].closed)

    def test_child_exec_does_not_create_a_manager(self) -> None:
        class Executed(Exception):
            pass

        calls: list[tuple[str, tuple[str, ...], Mapping[str, str]]] = []
        directories: list[str | os.PathLike[str]] = []

        def execute(file: str, args: Sequence[str], env: Mapping[str, str]) -> NoReturn:
            calls.append((file, tuple(args), env))
            raise Executed

        def chdir(path: str | os.PathLike[str]) -> None:
            directories.append(path)

        def no_manager(*, workspace: str | None = None, tmux_bin: str | None = None) -> NoReturn:
            raise AssertionError("child must not create another terminal manager")

        with patch.object(terminal, "TerminalManager", new=no_manager), \
             patch("pyclaude._env.bundled_cli", return_value="/bundled/claude"), \
             patch.object(os, "execvpe", new=execute), patch.object(os, "chdir", new=chdir):
            with self.assertRaises(Executed):
                terminal.main(["--child", "claude", "--workspace", str(ROOT), "--model", "sonnet"])
        self.assertEqual(directories, [ROOT])
        self.assertEqual(calls[0][:2], ("/bundled/claude", ("/bundled/claude", "--model", "sonnet")))

    def test_invalid_cli_options_fail_before_opening_screens(self) -> None:
        cases = (["agy", "--no-web", "--readonly"], ["agy", "--no-web", "--no-attach"],
                 ["agy", "--no-web", "--ttyd-bin", "/ttyd"], ["agy", "--no-web", "--web-port", "0"],
                 ["claude", "--capture", "/tmp/capture"], ["--child", "agy", "--capture", "/tmp/capture"],
                 ["agy", "--writable"], ["agy", "--no-capture"],
                 ["agy", "--web-port", "65536"], ["agy", "codex", "--web-port", "65535"], ["agy", "--capture-path", "/tmp/capture"],
                 ["--child", "agy", "codex"])
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                terminal.main(args)
            self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
