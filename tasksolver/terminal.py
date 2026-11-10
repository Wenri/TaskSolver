"""Open the bundled agent UIs in managed terminal screens.

The child launcher builds provider environments inside each screen. Credentials
stay in the inherited environment, never in tmux commands or launch files.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import select
import signal
import sys
import tempfile
import threading
import uuid
from collections.abc import Sequence
from contextlib import ExitStack
from types import FrameType
from typing import Literal, NoReturn

from wirecap.runtime.terminal import TerminalManager as TerminalManager
from wirecap.runtime.terminal import TerminalScreen as TerminalScreen

type Backend = Literal["agy", "codex", "claude", "kimi"]

__all__ = ["TerminalManager", "TerminalScreen", "open_terminal"]


def _backend(value: str) -> Backend:
    if value in ("agy", "codex", "claude", "kimi"):
        return value
    raise ValueError(f"Unknown terminal backend: {value!r}")


def open_terminal(backend: Backend, *, manager: TerminalManager,
                  name: str | None = None,
                  workspace: str | os.PathLike[str] | None = None,
                  model: str | None = None,
                  capture: bool | str = True,
                  sdk_socket: str | os.PathLike[str] | None = None) -> TerminalScreen:
    """Open an interactive UI without submitting a prompt or preapproving tools.

    Native backends capture JSONL in the manager's directory by default; pass a
    path to retain it elsewhere, or ``False`` to disable capture. Claude uses its
    native session store and always has ``capture_path=None``.
    Claude's bridged runtime publishes ``screen.control_socket`` for attachment
    through ``pyclaude.attach_terminal(screen)``. Its default socket is private
    to the manager; ``sdk_socket`` can select another private location.
    """
    backend = _backend(backend)
    if backend == "claude" and sdk_socket is None:
        sdk_socket = manager.directory / f"claude-{uuid.uuid4().hex}.sock"
    socket = _claude_control_path(backend, sdk_socket)
    workdir = Path(workspace).expanduser().resolve() if workspace is not None else manager.workspace
    if name is None:
        name = backend
        index = 2
        while name in manager.screens:
            name = f"{backend}-{index}"
            index += 1

    capture_path: Path | None = None
    temporary_capture = False
    if backend != "claude" and capture:
        if isinstance(capture, str):
            capture_path = Path(capture).expanduser().resolve()
            if any(screen.capture_path == capture_path for screen in manager.screens.values()):
                raise ValueError(f"Capture path already belongs to a screen: {capture_path}")
            capture_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            fd, path = tempfile.mkstemp(prefix=f"{backend}-", suffix=".jsonl", dir=manager.directory)
            os.close(fd)
            capture_path = Path(path)
            temporary_capture = True

    # Ignore inherited Python settings and exclude the workspace from imports:
    # its tasksolver.py must not shadow the installed child launcher.
    command = [sys.executable, "-E", "-P", "-m", "tasksolver.terminal", "--child", backend,
               "--workspace", os.fspath(workdir)]
    if model is not None:
        command += ["--model", model]
    if capture_path is not None:
        command += ["--capture-path", os.fspath(capture_path)]
    if socket is not None:
        command += ["--sdk-socket", os.fspath(socket)]
    try:
        with manager._lock:
            if socket is not None and any(
                    screen.control_socket == socket for screen in manager.screens.values()):
                raise ValueError(f"SDK socket path already belongs to a screen: {socket}")
            screen = manager.open(name, command, cwd=workdir, capture_path=capture_path)
            screen.control_socket = socket
            return screen
    except BaseException:
        if temporary_capture and capture_path is not None:
            capture_path.unlink(missing_ok=True)
        raise


def _claude_control_path(backend: Backend,
                         socket: str | os.PathLike[str] | None) -> Path | None:
    if backend != "claude" and socket is not None:
        raise ValueError("sdk_socket requires the Claude backend")
    socket_path = Path(os.path.abspath(Path(socket).expanduser())) if socket is not None else None
    if socket_path is not None:
        socket_path = socket_path.parent.resolve() / socket_path.name
    if socket_path is not None and (socket_path.exists() or socket_path.is_symlink()):
        raise FileExistsError(f"SDK socket path already exists: {socket_path}")
    return socket_path


def _child_command(backend: Backend, workspace: Path, model: str | None,
                   capture_path: Path | None, *,
                   sdk_socket: str | os.PathLike[str] | None = None) -> tuple[list[str], dict[str, str]]:
    socket = _claude_control_path(backend, sdk_socket)
    # A terminal screen is independent of any multiprocessing capture worker
    # or linked CLI that launched the parent Python.
    base = {key: value for key, value in os.environ.items()
            if not key.startswith(("WIRE_", "AGY_PROC_"))
            and key not in ("PYTHONHOME", "TASKSOLVER_CLAUDE_SOCKET")}
    base["PWD"] = os.fspath(workspace)
    capture = capture_path if capture_path is not None else Path(os.devnull)

    if backend == "agy":
        from pyagy._env import _vendored, linked_argv, linked_env

        env = linked_env(capture, base=base)
        argv = linked_argv(_vendored("vendor/agy", "../vendor/agy"),
                           ["--model", model] if model is not None else [], env=env)
    elif backend == "codex":
        from pycodex._env import codex_argv, linked_env

        env = linked_env(capture, base=base)
        argv = codex_argv(None, os.fspath(workspace), model=model, persistent=True)
    elif backend == "kimi":
        from pykimi._env import kimi_argv, linked_env

        env = linked_env(capture, base=base, model=model)
        argv = kimi_argv(None, persistent=True)
    else:
        from pyclaude._env import bundled_cli
        from pyclaude.client import PARENT_SESSION_ENV

        remove = {*PARENT_SESSION_ENV, "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                  "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS"}
        env = {key: value for key, value in base.items()
               if key not in remove and not key.startswith(
                   ("CLAUDE_AGENT_SDK_", "CLAUDE_CODE_SDK_", "CLAUDE_CODE_ENABLE_SDK_"))}
        if socket is not None:
            env["TASKSOLVER_CLAUDE_SOCKET"] = os.fspath(socket)
        return [bundled_cli(), *(["--model", model] if model is not None else [])], env

    env["PYTHONHOME"] = sys.prefix
    if capture_path is None:
        # agy gates capture on getenv presence, so setting its flag to "0"
        # would still start the bridge.
        env = {key: value for key, value in env.items()
               if not key.startswith(("WIRE_", "AGY_PROC_"))}
    return argv, env


class _Arguments(argparse.Namespace):
    backends: list[str]
    workspace: str | None
    model: str | None
    capture: str | None
    web_port: int | None
    no_web: bool
    readonly: bool
    no_attach: bool
    tmux_bin: str | None
    ttyd_bin: str | None
    child: bool
    capture_path: str | None
    sdk_socket: str | None


def _enter(manager: TerminalManager) -> bool:
    """Wait for a line on stdin; False once the screens have ended instead."""
    while not manager.wait(close_exited=True, timeout=0.2):
        if select.select([0], [], [], 0)[0]:
            if os.read(0, 4096):
                return True
            # Stdin has ended: there is nobody to ask, so only the screens end the launcher.
            return not manager.wait(close_exited=True)
    return False


def _attach(manager: TerminalManager, *, ask_first: bool) -> None:
    """Attach this terminal on request, as often as it is detached, until the screens end."""
    ask = ask_first
    while not manager.wait(close_exited=True, timeout=0):
        if ask:
            print("Press Enter to attach this terminal (Ctrl-b d detaches it again), "
                  "Ctrl-C to close the screens.", flush=True)
            if not _enter(manager):
                return
            if manager.wait(close_exited=True, timeout=0):
                return
        _ = manager.attach(close_exited=True)
        ask = True


def _serve(args: _Arguments) -> int:
    captured = [backend for backend in args.backends if backend != "claude"]
    with TerminalManager(workspace=args.workspace, tmux_bin=args.tmux_bin) as manager:
        capture_base = Path(args.capture) if args.capture is not None else None
        for position, backend in enumerate(args.backends):
            # A page shows the live capture, so a screen with a page always has one;
            # without --capture it is a temporary file that goes with the manager.
            capture: bool | str = not args.no_web
            if capture_base is not None and backend != "claude":
                if len(captured) == 1:
                    capture = os.fspath(capture_base)
                else:
                    # The screen's name: agy, agy-2, ...
                    count = args.backends[:position + 1].count(backend)
                    label = backend if count == 1 else f"{backend}-{count}"
                    capture = os.fspath(capture_base.with_name(
                        f"{capture_base.stem}-{label}{capture_base.suffix}"))
            screen = open_terminal(_backend(backend), manager=manager, model=args.model,
                                   capture=capture,
                                   sdk_socket=args.sdk_socket if backend == "claude" else None)
            if screen.capture_path is not None:
                print(f"{screen.name}: capture {screen.capture_path}", flush=True)
        if args.no_web:
            try:
                _attach(manager, ask_first=False)
            except KeyboardInterrupt:
                pass
            return 0

        from wirecap.runtime.webpage import WebScreen

        with ExitStack() as viewers:
            for index, name in enumerate(manager.screens):
                port = args.web_port + index if args.web_port else 0
                web = viewers.enter_context(WebScreen(
                    manager, screen=name, port=port, writable=not args.readonly, ttyd_bin=args.ttyd_bin))
                print(f"{name}: {web.url}", flush=True)
            try:
                if not args.no_attach and os.isatty(0) and os.isatty(1):
                    # The attached terminal covers the URLs: attach only when asked to.
                    _attach(manager, ask_first=True)
                elif not manager.wait(close_exited=True, timeout=0):
                    print("Press Ctrl-C to close the pages and screens.", flush=True)
                    # Ends by itself once every agent has exited, or the tmux server has.
                    _ = manager.wait(close_exited=True)
            except KeyboardInterrupt:
                pass
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backends", nargs="+", choices=("agy", "codex", "claude", "kimi"))
    parser.add_argument("--workspace", help="Working directory for the screens (default: current directory)")
    parser.add_argument("--model", help="Model selector passed to each chosen backend")
    parser.add_argument("--capture", metavar="PATH",
                        help="Keep the native JSONL capture in this file (default: a temporary file "
                             "for the web page, none with --no-web; several captured screens write "
                             "PATH with the screen name before its suffix)")
    parser.add_argument("--web-port", type=int,
                        help="First of the consecutive loopback ports serving one page per screen "
                             "(default 0: available ports)")
    parser.add_argument("--no-web", action="store_true", help="Serve no web terminal")
    parser.add_argument("--readonly", action="store_true", help="Refuse typing in the web terminal")
    parser.add_argument("--no-attach", action="store_true", help="Use only the web terminal")
    parser.add_argument("--tmux-bin", help="Path to the tmux executable")
    parser.add_argument("--ttyd-bin", help="Path to ttyd")
    parser.add_argument("--sdk-socket", help="Private Claude SDK socket path (default: manager directory)")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--capture-path", help=argparse.SUPPRESS)
    args = parser.parse_args(argv, namespace=_Arguments())
    if args.sdk_socket is not None and args.backends.count("claude") != 1:
        parser.error("--sdk-socket requires exactly one Claude screen")
    if args.no_web:
        for option, given in (("--web-port", args.web_port is not None), ("--readonly", args.readonly),
                              ("--no-attach", args.no_attach), ("--ttyd-bin", args.ttyd_bin is not None)):
            if given:
                parser.error(f"{option} cannot be combined with --no-web")
    captured = [backend for backend in args.backends if backend != "claude"]
    if args.capture is not None and not captured and not args.child:
        parser.error("--capture needs an agy, codex or kimi screen (Claude keeps its own session store)")
    if args.web_port is not None and not 0 <= args.web_port <= 65535:
        parser.error("--web-port must be between 0 and 65535 (0 selects an available port)")
    if args.web_port and args.web_port + len(args.backends) - 1 > 65535:
        parser.error("--web-port leaves too few ports for all screens")
    if args.child:
        if len(args.backends) != 1 or args.web_port is not None or args.capture is not None:
            parser.error("--child requires exactly one backend, no web terminal and no --capture")
        workspace = Path(args.workspace or os.getcwd()).expanduser().resolve()
        command, env = _child_command(_backend(args.backends[0]), workspace, args.model,
                                      Path(args.capture_path) if args.capture_path else None,
                                      sdk_socket=args.sdk_socket)
        os.chdir(workspace)
        os.execvpe(command[0], command, env)
    if args.capture_path is not None:
        parser.error("--capture-path is only used by the screen launcher")

    # A terminated launcher (kill, a closed terminal window) closes its screens like Ctrl-C does,
    # instead of leaving the tmux server and the agents behind.
    def interrupt(signum: int, frame: FrameType | None) -> NoReturn:
        del signum, frame
        raise KeyboardInterrupt

    with ExitStack() as handlers:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGHUP):
                _ = handlers.callback(signal.signal, number, signal.signal(number, interrupt))
        return _serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
