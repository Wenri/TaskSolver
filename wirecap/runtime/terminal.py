"""Named tmux screens owned by one Python process, with no desktop dependency."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import termios
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Self

__all__ = ["TerminalManager", "TerminalScreen"]

# A packager's tmux by absolute path (the Nix package pins nixpkgs'); None looks it up on PATH.
TMUX_BIN: str | None = None

_SCREEN_ENV = "TASKSOLVER_TERMINAL_SCREEN"
_SESSION = "tasksolver"
_FORMAT = "#{window_id}\t#{pane_id}"


def _process_start(pid: int) -> int | None:
    """Read a process identity, excluding zombies that have already exited."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def _owned_processes(token: str) -> dict[int, int]:
    """Find this screen's descendants even after reparenting or setsid().

    The marker is an ownership label, not a sandbox: commands must preserve it.
    Checking identities again before signals avoids acting on reused PIDs.
    """
    marker = f"{_SCREEN_ENV}={token}".encode()
    owned: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            if marker not in (entry / "environ").read_bytes().split(b"\0"):
                continue
        except OSError:
            continue
        pid = int(entry.name)
        identity = _process_start(pid)
        if identity is not None:
            owned[pid] = identity
    return owned


def _signal_processes(processes: Mapping[int, int], sig: signal.Signals) -> None:
    for pid, identity in processes.items():
        if _process_start(pid) != identity:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _stop_screen_processes(token: str) -> None:
    if not token:
        return
    signalled: dict[int, int] = {}
    deadline = time.monotonic() + 0.4
    while True:
        owned = _owned_processes(token)
        if not owned:
            return
        fresh = {pid: identity for pid, identity in owned.items()
                 if signalled.get(pid) != identity}
        _signal_processes(fresh, signal.SIGTERM)
        signalled.update(fresh)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    _signal_processes(owned, signal.SIGKILL)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        owned = _owned_processes(token)
        if not owned:
            return
        _signal_processes(owned, signal.SIGKILL)
        time.sleep(0.02)
    raise RuntimeError("terminal screen processes did not stop")


class TerminalScreen:
    """One retained tmux window. Exited commands keep their output until close()."""

    def __init__(self, manager: TerminalManager, name: str, pane_id: str, window_id: str, *,
                 capture_path: Path | None = None, token: str = "") -> None:
        self._manager = manager
        self.name = name
        self.pane_id = pane_id
        self.window_id = window_id
        self.target = pane_id
        self.capture_path = capture_path
        self.control_socket: Path | None = None
        self._token = token
        self._closed = False
        self._exit_status: int | None = None

    def _check_open(self) -> None:
        self._manager._check_open()
        if self._closed:
            raise RuntimeError(f"terminal screen {self.name!r} is closed")

    def _status(self) -> tuple[bool, int | None]:
        if self._closed:
            return False, self._exit_status
        self._check_open()
        value = self._manager._run([
            "display-message", "-p", "-t", self.pane_id,
            "#{pane_dead}\t#{pane_dead_status}\t#{pane_dead_signal}",
        ]).stdout.decode().strip("\n").split("\t")
        if value[0] == "0":
            return True, None
        status = int(value[1]) if len(value) > 1 and value[1] else None
        if len(value) > 2 and value[2]:
            status = -int(value[2])
        self._exit_status = status
        return False, status

    @property
    def alive(self) -> bool:
        with self._manager._lock:
            return self._status()[0]

    @property
    def exit_status(self) -> int | None:
        with self._manager._lock:
            return self._status()[1]

    def send(self, text: str, enter: bool = False) -> None:
        """Paste exact UTF-8 text without putting prompt content in process arguments."""
        with self._manager._lock:
            self._check_open()
            if not self._status()[0]:
                raise RuntimeError(f"terminal screen {self.name!r} has exited")
            if text:
                buffer = f"tasksolver-{uuid.uuid4().hex}"
                try:
                    self._manager._run(["load-buffer", "-b", buffer, "-"], data=text.encode())
                    self._manager._run(["paste-buffer", "-p", "-r", *self._manager._paste_flags,
                                        "-d", "-b", buffer, "-t", self.pane_id])
                finally:
                    self._manager._run(["delete-buffer", "-b", buffer], check=False)
            if enter:
                self._manager._run(["send-keys", *self._manager._key_flags,
                                    "-t", self.pane_id, "Enter"])

    def submit(self, text: str) -> None:
        self.send(text, enter=True)

    def send_keys(self, *keys: str) -> None:
        with self._manager._lock:
            self._check_open()
            if keys:
                self._manager._run(["send-keys", *self._manager._key_flags,
                                    "-t", self.pane_id, "--", *keys])

    def snapshot(self, history: int = 0) -> str:
        """Return the visible pane plus up to ``history`` preceding lines."""
        if history < 0:
            raise ValueError("history must be nonnegative")
        with self._manager._lock:
            self._check_open()
            return self._manager._run([
                "capture-pane", "-p", "-J", "-t", self.pane_id, "-S", str(-history),
            ]).stdout.decode("utf-8", "replace")

    def close(self) -> None:
        with self._manager._lock:
            if self._closed:
                return
            self._closed = True
            self._manager._screens.pop(self.name, None)
            callbacks = self._manager._screen_cleanups.pop(self.window_id, [])
            if not self._manager._screens:
                callbacks.extend(self._manager._cleanups)
                self._manager._cleanups = []
        with ExitStack() as cleanup:
            cleanup.callback(self._manager._run, ["kill-window", "-t", self.window_id], check=False)
            cleanup.callback(_stop_screen_processes, self._token)
            # Close this screen's viewers before removing its shared tmux window.
            for callback in callbacks:
                cleanup.callback(callback)


class TerminalManager:
    """Own an isolated tmux server, named screens, and optional attached viewers.

    ``attach`` uses the current terminal; it never opens a desktop window. Screen
    commands inherit the server environment, plus an ownership marker used to reap
    descendants that ignore terminal hangup or create their own process groups.
    """

    def __init__(self, *, workspace: str | os.PathLike[str] | None = None,
                 tmux_bin: str | None = None, env: Mapping[str, str] | None = None,
                 width: int = 120, height: int = 40) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("terminal dimensions must be positive")
        self.workspace = Path(workspace or Path.cwd()).expanduser().resolve(strict=True)
        if not self.workspace.is_dir():
            raise NotADirectoryError(self.workspace)
        self.width, self.height = width, height
        self._env = dict(os.environ if env is None else env)
        self._env.pop("TMUX", None)
        self._env.pop("TMUX_PANE", None)
        executable = shutil.which(tmux_bin or TMUX_BIN or "tmux", path=self._env.get("PATH"))
        if executable is None:
            raise FileNotFoundError(f"tmux executable not found: {tmux_bin or TMUX_BIN or 'tmux'}")
        self.tmux_bin = str(Path(executable).resolve())
        self._lock = threading.RLock()
        self._screens: dict[str, TerminalScreen] = {}
        self._cleanups: list[Callable[[], None]] = []
        self._screen_cleanups: dict[str, list[Callable[[], None]]] = {}
        self._closed = False
        self._server: subprocess.Popen[bytes] | None = None
        self._directory = tempfile.TemporaryDirectory(prefix="tasksolver-term-")
        self.directory = Path(self._directory.name)
        self.socket_path = self.directory / "tmux.sock"
        try:
            self._server = subprocess.Popen(
                [self.tmux_bin, "-D", "-f", "/dev/null", "-S", str(self.socket_path)],
                cwd=self.workspace, env=self._env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
            )
            self._wait_server()
            for option, value in (
                ("exit-empty", "off"), ("exit-unattached", "off"),
                ("remain-on-exit", "on"), ("remain-on-exit-format", ""),
                ("history-limit", "10000"),
                ("automatic-rename", "off"), ("allow-rename", "off"),
                ("renumber-windows", "on"), ("status-left", "TaskSolver "),
                ("status-right", ""), ("set-titles", "off"),
            ):
                self._run(["set-option", "-g", option, value])
            shell = shutil.which("sh", path=self._env.get("PATH"))
            if shell is None:
                raise FileNotFoundError("a POSIX sh executable is required to launch terminal commands")
            self._run(["set-option", "-g", "default-shell", shell])
            # tmux 3.7 sanitizes control characters by default; older versions paste them unchanged.
            commands = self._run(["list-commands"]).stdout.decode()
            paste = re.search(r"^paste-buffer .*?\[-([A-Za-z]+)\]", commands, re.MULTILINE)
            self._paste_flags = ["-S"] if paste is not None and "S" in paste[1] else []
            # A programmatic write has no attached-client identity. Otherwise tmux
            # can select a read-only observer and reject send-keys on its behalf.
            keys = next((line for line in commands.splitlines() if line.startswith("send-keys ")), "")
            self._key_flags = ["-c", ""] if "-c target-client" in keys else []
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"Terminal cleanup also failed: {cleanup_error!r}")
            raise

    def _wait_server(self) -> None:
        assert self._server is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._server.poll() is not None:
                _, error = self._server.communicate()
                raise RuntimeError(f"tmux server failed: {error.decode('utf-8', 'replace').strip()}")
            if self.socket_path.exists():
                return
            time.sleep(0.01)
        raise TimeoutError("tmux server did not create its private socket")

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("terminal manager is closed")
        if self._server is None or self._server.poll() is not None:
            raise RuntimeError("terminal manager's tmux server has exited")

    def _run(self, args: Sequence[str], *, data: bytes | None = None,
             check: bool = True) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            # tmux parses a trailing semicolon as a command separator, even in a separate argv item.
            [self.tmux_bin, "-S", str(self.socket_path),
             *(arg[:-1] + "\\;" if arg.endswith(";") else arg for arg in args)], input=data,
            stdin=subprocess.DEVNULL if data is None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env,
            cwd=self.workspace, timeout=5,
        )
        if check and result.returncode:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"tmux {args[0]} failed: {message}")
        return result

    @property
    def screens(self) -> Mapping[str, TerminalScreen]:
        with self._lock:
            return MappingProxyType(dict(self._screens))

    def open(self, name: str, command: Sequence[str], *,
             cwd: str | os.PathLike[str] | None = None,
             capture_path: Path | None = None) -> TerminalScreen:
        if not name or not name.isprintable():
            raise ValueError("screen name must be nonempty printable text")
        if not command or isinstance(command, str) or any("\0" in arg for arg in command):
            raise ValueError("command must be a nonempty sequence of arguments without NUL bytes")
        workdir = Path(cwd).expanduser().resolve(strict=True) if cwd is not None else self.workspace
        if not workdir.is_dir():
            raise NotADirectoryError(workdir)
        with self._lock:
            self._check_open()
            if name in self._screens:
                raise ValueError(f"terminal screen {name!r} already exists")
            token = uuid.uuid4().hex
            existing = {screen.window_id for screen in self._screens.values()}
            if self._screens:
                args = ["new-window", "-d", "-t", _SESSION]
            else:
                args = ["new-session", "-d", "-s", _SESSION,
                        "-x", str(self.width), "-y", str(self.height)]
            args += ["-n", name, "-c", str(workdir), "-e", f"{_SCREEN_ENV}={token}",
                     "-P", "-F", _FORMAT, "exec " + shlex.join(command)]
            try:
                result = self._run(args).stdout.decode().strip().split("\t")
                if len(result) != 2 or not result[0].startswith("@") or not result[1].startswith("%"):
                    raise RuntimeError("tmux returned an invalid window identity")
                window, pane = result
                screen = TerminalScreen(self, name, pane, window, capture_path=capture_path, token=token)
                self._screens[name] = screen
                return screen
            except BaseException:
                _stop_screen_processes(token)
                # A server-side create can succeed before the client or identity read fails.
                windows = self._run(["list-windows", "-t", _SESSION, "-F", "#{window_id}"], check=False)
                for window in windows.stdout.decode().splitlines():
                    if window.startswith("@") and window not in existing:
                        self._run(["kill-window", "-t", window], check=False)
                raise

    def attach_argv(self, screen: str | None = None, read_only: bool = False) -> list[str]:
        with self._lock:
            self._check_open()
            if not self._screens:
                raise RuntimeError("open a terminal screen before attaching")
            if screen is not None:
                selected = self._screens[screen]
                self._run(["select-window", "-t", f"{_SESSION}:{selected.window_id}"])
            return [self.tmux_bin, "-S", str(self.socket_path), "attach-session", "-E",
                    *(["-r"] if read_only else []), "-t", _SESSION]

    def attach(self, screen: str | None = None, read_only: bool = False) -> int:
        if not os.isatty(0) or not os.isatty(1):
            raise RuntimeError("attach requires the current stdin and stdout to be a terminal; use attach_argv or a web viewer")
        previous = termios.tcgetattr(0)
        try:
            return subprocess.run(self.attach_argv(screen, read_only), env=self._env, check=False).returncode
        finally:
            termios.tcsetattr(0, termios.TCSANOW, previous)

    def _create_view(self, screen: str | None = None, read_only: bool = True) -> tuple[list[str], Callable[[], None]]:
        with self._lock:
            self._check_open()
            if not self._screens:
                raise RuntimeError("open a terminal screen before attaching a viewer")
            selected = self._screens[screen] if screen is not None else None
            view = f"view-{uuid.uuid4().hex}"
            closed = False

            def close_view() -> None:
                nonlocal closed
                with self._lock:
                    if not closed:
                        closed = True
                        self._run(["kill-session", "-t", view], check=False)

            try:
                if selected is None:
                    self._run(["new-session", "-d", "-s", view, "-t", _SESSION])
                else:
                    self._run(["new-session", "-d", "-s", view,
                               "-x", str(self.width), "-y", str(self.height), ""])
                    self._run(["link-window", "-k", "-s", selected.window_id, "-t", f"{view}:0"])
                    # A dedicated page sends keys to its backend, without tmux's
                    # prefix shortcuts for switching to another session/window.
                    for option, value in (("prefix", "None"), ("prefix2", "None"), ("status", "off")):
                        self._run(["set-option", "-t", view, option, value])
            except BaseException:
                close_view()
                raise
            return ([self.tmux_bin, "-S", str(self.socket_path), "attach-session", "-E",
                     *(["-r"] if read_only else []), "-t", view], close_view)

    def _register_cleanup(self, callback: Callable[[], None], *, screen: TerminalScreen | None = None) -> None:
        with self._lock:
            self._check_open()
            if screen is None:
                self._cleanups.append(callback)
            else:
                if self._screens.get(screen.name) is not screen:
                    raise RuntimeError("viewer screen is no longer open")
                self._screen_cleanups.setdefault(screen.window_id, []).append(callback)

    def _stop_server(self) -> None:
        server = self._server
        if server is None:
            return
        try:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=2)
        finally:
            if server.stderr is not None:
                server.stderr.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            screens = list(self._screens.values())
            callbacks, self._cleanups = self._cleanups, []
            for screen_callbacks in self._screen_cleanups.values():
                callbacks.extend(screen_callbacks)
            self._screen_cleanups.clear()
        # Stop viewers first; always continue through panes, server and directory cleanup.
        with ExitStack() as cleanup:
            cleanup.callback(self._directory.cleanup)
            cleanup.callback(self._stop_server)
            for screen in screens:
                cleanup.callback(screen.close)
            for callback in callbacks:
                cleanup.callback(callback)

    def __enter__(self) -> Self:
        self._check_open()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()
