"""AgyProcess — the ``multiprocessing.Process`` for launching the agy CLI. Always instrumented
(preloaded shim + capture JSONL, on the pinned vendor/agy) and always runs an in-agy worker target.

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
`popen_fork.Popen` (shaped like `popen_spawn_posix`) that execs agy under a PTY (not python), owns
the PTY + the lifecycle + fd inheritance, and runs `mp_child._bootstrap`. `start/join/exitcode/
terminate` are inherited and track agy's pid; the answer flows over the caller's SimpleQueue, the
transcript over the PTY (a byproduct on ``transcript``).
"""
import time

from wirecap.runtime.process import WireProcess

from . import conversations as _conv
from ._pty import PtyPopen


def _agy_argv(prompt, persistent, model, skip_permissions, extra_flags,
              conversation_id, continue_latest):
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


class AgyProcess(WireProcess):
    """`WireProcess` handle for an agy run (always instrumented, always a worker). Like stock
    ``Process`` it does not own the result channel: the caller passes a ``SimpleQueue`` via
    ``args=(q,)`` and drains it (see client.py's collect/ask/collect_many, which use ``service_pty``
    + ``q.get()``). ``reap``/``exit_status``/``close`` are inherited from ``WireProcess``; the PTY
    transcript is a byproduct on ``transcript`` (used for crash/fallback diagnostics)."""

    @staticmethod
    def _Popen(process_obj):
        return PtyPopen(process_obj)

    def __init__(self, target=None, name=None, args=(), kwargs=None, *,
                 agy_bin=None, agy_args=None, prompt=None, model=None,
                 skip_permissions=False, extra_flags=None, persistent=False,
                 conversation_id=None, continue_latest=False, workdir=None,
                 capture=None, data_dir=None, trust=True, extra_env=None, echo=False,
                 daemon=None):
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
        self._trust = trust            # pre-trust the workspace (no folder-trust prompt)
        self._extra_env = extra_env    # caller overlays layered onto instrumented_env (shim knobs)
        self._echo = echo              # mirror agy's PTY output to our stdout (debug)

    # --- PTY service passthroughs (the caller owns the result queue and drains it via these) ---
    def service_pty(self, timeout, readers):
        """Drain agy's PTY (+ auto-answer) while waiting up to ``timeout`` s for data on any of
        ``readers`` (the caller's result-queue read end(s)); True once one is ready. The caller
        (client.py) owns the queue and passes its reader here — this keeps agy's PTY drained in the
        same wait the caller uses to read results, so no background pump thread is needed."""
        return self._popen._service(timeout, readers)

    @property
    def last_output(self):
        """Wall-clock of the last PTY write — the turn-boundary idle signal the caller's ask-loop
        uses to detect a settled turn. Settable so the caller can reset it right after submitting a
        prompt (so the idle detector measures from the submit, not the prior turn)."""
        return self._popen._last_output

    @last_output.setter
    def last_output(self, ts):
        self._popen._last_output = ts

    @property
    def conversation_id(self):
        """agy's native conversation id for this run — the resume id, or the one captured
        after launch (from the shim's conversation_id event, else the newest store db).
        Persist it and pass ``conversation_id=`` to a new AgyProcess (or ``pyagy.resume``)
        to continue this conversation with full prior context."""
        if self._conversation_id is None and getattr(self, "_popen", None) is not None:
            self._conversation_id = _conv.capture_conversation_id(
                getattr(self._popen, "_snap", None),
                capture_path=getattr(self._popen, "_capture_path", None),
                home=getattr(self._popen, "_home", None))
        return self._conversation_id

    # The decoded-answer collection helpers (collect / ask-turn / collect_many) live in the caller
    # (client.py), which owns the result queue — this class is a Process, not its consumer. They
    # drain via ``service_pty(timeout, [q._reader])`` + ``q.get()``.

    # --- PTY: raw input + the transcript byproduct ---
    @property
    def transcript(self):
        """The full ANSI-stripped PTY transcript seen so far (diagnostics / fallback answer)."""
        return self._popen.transcript

    def write(self, data):
        """Write raw bytes to the PTY."""
        self._popen.write(data)

    def send_line(self, text):
        """Type a line + Enter into the PTY."""
        self._popen.send_line(text)

    def send(self, prompt):
        """Type + submit a prompt into agy's interactive TUI (fire-and-forget)."""
        self._popen.send_line(prompt)
        self._popen._last_output = time.time()

    @property
    def workspace(self):
        """The resolved git workspace agy ran in."""
        return getattr(self._popen, "_workspace", None)

    @property
    def home(self):
        """The scoped HOME for this run (data_dir scoping), or None for the global store."""
        return getattr(self._popen, "_home", None)
