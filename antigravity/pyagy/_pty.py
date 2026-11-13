"""PtyPopen — launch agy under a PTY as a multiprocessing spawn child, and BE the PTY handler.

`agy` inspects its controlling terminal and refuses to behave without a real TTY, so we fork it
under a pty; it also blocks on terminal-capability queries + the folder-trust menu until answered.

Everything generic about running a CLI under a pty — the fork, the winsize, reading/echoing the
master, the transcript byproduct, `_service`/`service_many`, and the PTY-master death sentinel —
lives in the shared `wirecap.runtime.pty.WirePtyPopen`; the generic spawn-child machinery (boot
channel, fd inheritance, lifecycle) lives under that in `wirecap.runtime.process.WirePopen`. This
subclass supplies `_resolve_launch` (the linked host's argv/env and scoped conversation
store), `_answer` (agy's terminal-query +
folder-trust auto-reply), and `_interrupt` (Ctrl-C the TUI on close).

The pinned vendor/agy host loads libagy.so directly and starts its capture worker. Like
`popen_spawn_posix`, this Popen owns the fork + fd inheritance but NOT the result queue: the caller
(client.py) creates the SimpleQueue, passes it as a target arg, and drains it via `_service`.
See `wirecap/decode/mp_child.py` (child side) + `wirecap/runtime/{process,pty}.py`.

`AgyProcess` (pyagy/agyprocess.py) is the user-facing `SpawnProcess` handle; this is its `_Popen`.
"""
from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from typing import Protocol, cast
from multiprocessing.process import BaseProcess

from wirecap.runtime.pty import WirePtyPopen, service_many as service_many

from . import conversations as _conv
from ._env import _vendored, linked_env, linked_argv
from ._term import answer_queries, answer_trust
from .conversations import ensure_git_workspace

class _AgyLaunch(Protocol):
    _agy_bin: str | None
    _agy_args: Sequence[str]
    _workdir: str | None
    _capture: str | None
    _data_dir: str | None
    _app_data_dir: str | None
    _account: str | None
    _isolated_auth: bool | None
    _trust: bool
    _extra_env: Mapping[str, str | None] | None

# the pinned agy whose build-id matches the shim: bundled pyagy/vendor/agy (wheel) or the
# sibling antigravity/vendor/agy (checkout) — never an external path.
_VENDOR_AGY = _vendored("vendor/agy", "../vendor/agy")


class PtyPopen(WirePtyPopen[BaseProcess]):
    """The `WirePtyPopen` for an agy run: execs agy under a PTY with agy's argv/env and answers its
    startup prompts. Constructed by `AgyProcess._Popen(process_obj)`; reads its config off
    `process_obj` (`_agy_bin`, `_agy_args`, `_workdir`, `_capture`, `_data_dir`, `_trust`,
    `_extra_env`, `_echo`). The PTY mechanics and the generic boot-channel/lifecycle are inherited."""
    method = "agy"

    # --- launch methods (fill the WirePtyPopen/WirePopen base) -----------------
    def _resolve_launch(self, process_obj: BaseProcess) -> tuple[list[str], dict[str, str], str]:
        """Build agy's linked argv + env (the base adds WIRE_MP_BOOT_FD). Records the run's
        resolved workspace/capture/home for AgyProcess's accessors."""
        # instrumentation needs the build-id-matched binary: an explicit programmatic agy_bin
        # (tests inject one), else the packaged agy (_VENDOR_AGY — bundled or sibling, never external).
        process = cast(_AgyLaunch, process_obj)
        agy = process._agy_bin or _VENDOR_AGY
        if not os.path.isfile(agy):
            # Redistributable bundles leave the host out (it links the proprietary agy).
            raise FileNotFoundError(f"the agy host is not built: {agy}; rebuild it with `python -m "
                                    "tasksolver.reconstruct --agy` (`pixi reinstall tasksolver` in a checkout)")
        workdir = ensure_git_workspace(process._workdir)
        self._workspace = workdir                            # resolved workspace (AgyProcess.workspace)
        capture = process._capture or os.path.join(workdir, "agy-capture.jsonl")
        self._capture_path = capture        # for AgyProcess.conversation_id (conversation_id event)
        account = getattr(process, "_account", None)
        isolated_auth = getattr(process, "_isolated_auth", None)
        self._app_data_dir = _conv.resolve_app_data_dir(
            getattr(process, "_app_data_dir", None), home=process._data_dir, account=account)
        self._home, env_ovr = _conv.scope_for_run(
            workdir, process._data_dir,
            trust=process._trust,
            app_data_dir=self._app_data_dir,
            isolated_auth=isolated_auth,
            account=account)                     # dedicated/scoped store + workspace trust
        # store/auth isolation defaults + caller overlays; a scoped HOME always wins.
        extra: dict[str, str | None] = {**env_ovr, **(process._extra_env or {})}
        if "HOME" in env_ovr:
            extra["HOME"] = env_ovr["HOME"]
        env = linked_env(capture=capture, extra_env=extra)
        agy_args = list(process._agy_args or ["--print", "agy-mp"])
        if (self._app_data_dir != _conv.GLOBAL_APP_DATA_DIR
                and not any(a == "--app_data_dir" or a.startswith("--app_data_dir=") for a in agy_args)):
            agy_args = [f"--app_data_dir={self._app_data_dir}", *agy_args]
        # An explicit empty LD_PRELOAD in extra_env opts out of instrumentation (e.g. baseline
        # auth probes).
        if env.pop("LD_PRELOAD", None) == "":
            env.pop("AGY_PROC_ENABLE", None)
            argv = [agy, *agy_args]
        else:
            argv = linked_argv(agy, agy_args, env=env)
        return argv, env, workdir

    def _spawn_child(self, argv: list[str], workdir: str, env: dict[str, str],
                     process_obj: BaseProcess) -> None:
        """agy's prompt-answering state + the pre-launch conversation-store snapshot, then the
        base's pty.fork. The snapshot MUST be taken before the fork (it diffs the store afterwards
        to recover this run's conversation id)."""
        self._qpos = 0                  # terminal-query scan position
        self._trust_answered = False
        self._snap = _conv.snapshot(app_data_dir=self._app_data_dir, home=self._home)   # pre-launch store snapshot → conversation_id
        super()._spawn_child(argv, workdir, env, process_obj)

    def _answer(self) -> None:
        """Reply to agy's terminal-capability queries + the folder-trust menu (else it blocks)."""
        self._qpos = answer_queries(self.raw, self._qpos, lambda b: os.write(self.fd, b))
        if not self._trust_answered:
            self._trust_answered = answer_trust(self.raw, lambda b: os.write(self.fd, b))

    def _interrupt(self) -> None:
        """WirePopen.close callback: Ctrl-C twice to break agy out of its TUI before SIGTERM."""
        for _ in range(2):
            try:
                self.write(b"\x03")
                time.sleep(0.2)
            except OSError:
                break
