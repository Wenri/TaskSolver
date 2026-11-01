"""Open the bundled agent UIs in managed terminal screens.

The child launcher builds provider environments inside each screen. Credentials
stay in the inherited environment, never in tmux commands or launch files.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
from collections.abc import Sequence
from typing import Literal

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
                  capture: bool | str = True) -> TerminalScreen:
    """Open an interactive UI without submitting a prompt or preapproving tools.

    Native backends capture JSONL in the manager's directory by default; pass a
    path to retain it elsewhere, or ``False`` to disable capture. Claude uses its
    native session store and always has ``capture_path=None``.
    """
    backend = _backend(backend)
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
    try:
        return manager.open(name, command, cwd=workdir, capture_path=capture_path)
    except BaseException:
        if temporary_capture and capture_path is not None:
            capture_path.unlink(missing_ok=True)
        raise


def _claude_binary() -> str:
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or spec.origin is None:
        raise FileNotFoundError("The bundled Claude SDK is missing; run pixi install")
    binary = Path(spec.origin).resolve().parent / "_bundled" / "claude"
    if not binary.is_file():
        raise FileNotFoundError(f"The bundled Claude CLI is missing: {binary}; run pixi install")
    return os.fspath(binary)


def _child_command(backend: Backend, workspace: Path, model: str | None,
                   capture_path: Path | None) -> tuple[list[str], dict[str, str]]:
    # A terminal screen is independent of any multiprocessing capture worker
    # or linked CLI that launched the parent Python.
    base = {key: value for key, value in os.environ.items()
            if not key.startswith(("WIRE_", "AGY_PROC_")) and key != "PYTHONHOME"}
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
        from pyclaude.client import PARENT_SESSION_ENV

        remove = {*PARENT_SESSION_ENV, "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                  "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS"}
        env = {key: value for key, value in base.items()
               if key not in remove and not key.startswith(
                   ("CLAUDE_AGENT_SDK_", "CLAUDE_CODE_SDK_", "CLAUDE_CODE_ENABLE_SDK_"))}
        return [_claude_binary(), *(["--model", model] if model is not None else [])], env

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
    no_capture: bool
    web_port: int | None
    writable: bool
    no_attach: bool
    tmux_bin: str | None
    ttyd_bin: str | None
    child: bool
    capture_path: str | None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backends", nargs="+", choices=("agy", "codex", "claude", "kimi"))
    parser.add_argument("--workspace", help="Working directory for the screens (default: current directory)")
    parser.add_argument("--model", help="Model selector passed to each chosen backend")
    parser.add_argument("--no-capture", action="store_true", help="Disable native JSONL capture")
    parser.add_argument("--web-port", type=int, help="Serve a loopback web terminal on this port")
    parser.add_argument("--writable", action="store_true", help="Allow typing in the web terminal")
    parser.add_argument("--no-attach", action="store_true", help="Use only the web terminal (with --web-port)")
    parser.add_argument("--tmux-bin", help="Path to the tmux executable")
    parser.add_argument("--ttyd-bin", help="Path to ttyd (with --web-port)")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--capture-path", help=argparse.SUPPRESS)
    args = parser.parse_args(argv, namespace=_Arguments())
    if args.writable and args.web_port is None:
        parser.error("--writable requires --web-port")
    if args.no_attach and args.web_port is None:
        parser.error("--no-attach requires --web-port")
    if args.ttyd_bin is not None and args.web_port is None:
        parser.error("--ttyd-bin requires --web-port")
    if args.web_port is not None and not 0 <= args.web_port <= 65535:
        parser.error("--web-port must be between 0 and 65535 (0 selects an available port)")
    if args.child:
        if len(args.backends) != 1 or args.web_port is not None:
            parser.error("--child requires exactly one backend and no web terminal")
        workspace = Path(args.workspace or os.getcwd()).expanduser().resolve()
        command, env = _child_command(_backend(args.backends[0]), workspace, args.model,
                                      Path(args.capture_path) if args.capture_path else None)
        os.chdir(workspace)
        os.execvpe(command[0], command, env)
    if args.capture_path is not None:
        parser.error("--capture-path is only used by the screen launcher")

    with TerminalManager(workspace=args.workspace, tmux_bin=args.tmux_bin) as manager:
        for backend in args.backends:
            screen = open_terminal(_backend(backend), manager=manager, model=args.model,
                                   capture=not args.no_capture)
            if screen.capture_path is not None:
                print(f"{screen.name}: capture {screen.capture_path}", flush=True)
        if args.web_port is None:
            try:
                return manager.attach()
            except KeyboardInterrupt:
                return 0

        from wirecap.runtime.webterm import WebTerminal

        with WebTerminal(manager, port=args.web_port, writable=args.writable,
                         ttyd_bin=args.ttyd_bin) as web:
            print(web.url, flush=True)
            try:
                if not args.no_attach and os.isatty(0) and os.isatty(1):
                    manager.attach()
                    print(f"Web terminal remains available at {web.url}", flush=True)
                print("Press Ctrl-C to close viewers and screens.", flush=True)
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
