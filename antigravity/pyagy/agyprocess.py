"""AgyProcess — the ``multiprocessing.Process`` for launching the agy CLI. Always linked
(native host + capture worker, on the pinned vendor/agy) and always runs an in-agy worker target.

Modeled on stock ``Process``: it does NOT own the result channel. The **caller** creates the result
``SimpleQueue`` and passes it as a target arg — ``AgyProcess(target=stream_turns, args=(q,))`` — so
it rides ``process_obj``'s pickle, exactly like ``Process(target=f, args=(q,))``. The target (default
``wirecap.decode.mp_child.stream_turns``) runs inside agy's embedded interpreter and ``.put``s decoded
objects on that queue; the caller (client.py) drains it via ``service_pty(timeout, [q._reader])`` +
``q.get()``. The PTY transcript is drained as a byproduct and kept on ``transcript`` for diagnostics
(crashes, and a fallback answer when no turn decodes).

    # the caller owns the queue and drains it (see client.py for collect/ask/collect_many):
    from multiprocessing import get_context
    q = get_context("spawn").SimpleQueue()
    p = AgyProcess(prompt="What is 2+2?", args=(q,)); p.start(); q._writer.close()
    while p.service_pty(1.0, [q._reader]):
        obj = q.get()                       # decoded turns; ("_wire_done", code) on completion
        ...
    p.close()

Design: `AgyProcess` is a `SpawnProcess` whose `_Popen` is `_pty.PtyPopen` — a custom
`popen_spawn_posix.Popen` that execs agy under a PTY (not python), owns
the PTY + the lifecycle + fd inheritance, and runs `mp_child._bootstrap`. `start/join/exitcode/
terminate` are inherited and track agy's pid; the answer flows over the caller's SimpleQueue, the
transcript over the PTY (a byproduct on ``transcript``).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from multiprocessing.process import BaseProcess

from wirecap.runtime.process import Target
from wirecap.runtime.pty import WirePtyProcess

from . import conversations as _conv
from ._pty import PtyPopen


def _agy_argv(prompt: str | None, persistent: bool, model: str | None,
              skip_permissions: bool, extra_flags: Sequence[str] | None,
              conversation_id: str | None, continue_latest: bool) -> list[str]:
    """agy's argv tail (everything after the binary). One-shot uses ``--print``, persistent
    uses ``--prompt-interactive``; ``conversation_id`` / ``continue_latest`` resume a stored
    conversation (both compose with either flag)."""
    flag = "--prompt-interactive" if persistent else "--print"
    argv = [flag, prompt if prompt is not None else "agy-mp"]
    if model:
        argv += ["--model", model]
    if conversation_id:
        argv.append(f"--conversation={conversation_id}")
    elif continue_latest:
        argv.append("--continue")
    if skip_permissions:
        argv.append("--dangerously-skip-permissions")
    if extra_flags:
        argv += list(extra_flags)
    return argv


class AgyProcess(WirePtyProcess[PtyPopen]):
    """`WireProcess` handle for an agy run (always linked, always a worker). Like stock
    ``Process`` it does not own the result channel: the caller passes a ``SimpleQueue`` via
    ``args=(q,)`` and drains it (see client.py's collect/ask/collect_many, which use ``service_pty``
    + ``q.get()``). ``reap``/``exit_status``/``close`` are inherited from ``WireProcess``; the PTY
    transcript is a byproduct on ``transcript`` (used for crash/fallback diagnostics)."""

    @staticmethod
    def _Popen(process_obj: BaseProcess) -> PtyPopen:
        return PtyPopen(process_obj)

    def __init__(self, target: Target | None = None, name: str | None = None,
                 args: tuple[object, ...] = (), kwargs: Mapping[str, object] | None = None, *,
                 agy_bin: str | None = None, agy_args: Sequence[str] | None = None,
                 prompt: str | None = None, model: str | None = None,
                 skip_permissions: bool = False, extra_flags: Sequence[str] | None = None,
                 persistent: bool = False, conversation_id: str | None = None,
                 continue_latest: bool = False, workdir: str | None = None,
                 capture: str | None = None, data_dir: str | None = None,
                 app_data_dir: str | None = None, trust: bool = True,
                 extra_env: Mapping[str, str | None] | None = None, echo: bool = False,
                 daemon: bool | None = None) -> None:
        super().__init__(target=target, name=name, args=args, kwargs=kwargs, daemon=daemon)
        # argv: an explicit agy_args tail wins; else assemble it from the flags. One-shot uses
        # `agy --print <prompt>`; persistent uses `--prompt-interactive` (drive via .ask()/.send()).
        self._agy_args = agy_args if agy_args is not None else _agy_argv(
            prompt, persistent, model, skip_permissions, extra_flags,
            conversation_id, continue_latest)
        self._agy_bin = agy_bin        # agy binary (default: the pinned vendor/agy)
        self._workdir = workdir        # git workspace (default: a throwaway repo)
        self._capture = capture
        self._persistent = persistent  # long-lived interactive agy (drive via .ask()/.send())
        self._conversation_id = conversation_id  # resume id; else captured after first turn
        self._data_dir = data_dir      # scope the conversation store to a project repo
        self._app_data_dir = app_data_dir  # subdirectory under ~/.gemini (default: pyagy)
        self._trust = trust            # pre-trust the workspace (no folder-trust prompt)
        self._extra_env = extra_env    # caller overlays layered onto linked_env (shim knobs)
        self._echo = echo              # mirror agy's PTY output to our stdout (debug)

    # service_pty / last_output / transcript / write / send_line / send / workspace are inherited
    # from WirePtyProcess — the PTY-facing surface is identical for every CLI under a pty.

    @property
    def conversation_id(self) -> str | None:
        """agy's native conversation id for this run — the resume id, or the one captured
        after launch (from the shim's conversation_id event, else the newest store db).
        Persist it and pass ``conversation_id=`` to a new AgyProcess (or ``pyagy.resume``)
        to continue this conversation with full prior context."""
        if self._conversation_id is None and self._popen is not None:
            self._conversation_id = _conv.capture_conversation_id(
                self._popen._snap,
                capture_path=self._popen._capture_path,
                app_data_dir=self._popen._app_data_dir,
                home=self._popen._home)
        return self._conversation_id

    # The decoded-answer collection helpers (collect / ask-turn / collect_many) live in the caller
    # (client.py), which owns the result queue — this class is a Process, not its consumer. They
    # drain via ``service_pty(timeout, [q._reader])`` + ``q.get()``.

    @property
    def home(self) -> _conv.PathInput | None:
        """The scoped HOME for this run (data_dir scoping), or None for the global ~/.gemini root."""
        return self._popen._home if self._popen is not None else None

    @property
    def app_data_dir(self) -> str | None:
        """The resolved ``app_data_dir`` name under ``.gemini/`` for this run."""
        return self._popen._app_data_dir if self._popen is not None else self._app_data_dir
