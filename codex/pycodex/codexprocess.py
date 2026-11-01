"""CodexProcess / CodexPopen — codex driven as a wirecap mp-child, streaming decoded ``codex_turn``s
home over a caller-owned ``SimpleQueue``.

The codex sibling of pyagy's AgyProcess/PtyPopen, on the shared ``wirecap.runtime.pty`` bases — so
``codex exec`` and ``agy --print`` (and the two TUI modes, and the two resumes) run on identical
machinery. CodexPopen forks under a pty like agy's, with two deliberate deviations: the one-shot
points the child's stdin at ``/dev/null`` (``codex exec`` blocks reading stdin, and unlike the TUI
there is nothing to type into it), and the death sentinel stays ``os.pidfd_open`` rather than the
pty master — codex spawns tool grandchildren (shells/apply_patch) that inherit the slave, so the
master can outlive codex itself. The embedded wirecap bridge inside codex runs
``wirecap.decode.mp_child``, whose ``stream_turns`` target ``.put``s ``codex_turn``s over the queue;
the durable ``WIRE_CAPTURE`` JSONL stays authoritative for the returned turns (see client.py).
"""
from __future__ import annotations

import os
import re
import tempfile

from collections.abc import Mapping, Sequence
from multiprocessing.process import BaseProcess
from typing import Protocol, cast

from wirecap.runtime.pty import WirePtyPopen, WirePtyProcess, strip_ansi
from wirecap.runtime.process import Target

from ._env import codex_argv, linked_env


class _CodexLaunch(Protocol):
    _workdir: str | None
    _capture: str | None
    _prompt: str | None
    _model: str | None
    _extra_flags: Sequence[str] | None
    _codex_bin: str | None
    _extra_env: Mapping[str, str] | None
    _persistent: bool
    _session_id: str | None
    _continue_latest: bool
    _codex_home: str | None
    _prompt_via_stdin: bool


def _prompt_stdin_fd(prompt: str | None) -> int:
    """An fd 0 for ``codex exec -``: an UNLINKED temp file holding the prompt.

    A file (not a pipe) so any prompt size works without a writer thread or the
    64KB pipe-capacity deadlock; unlinked so the last close reclaims it; not the
    pty slave so nothing is echoed into the transcript and canonical-mode line
    limits never apply. codex reads it to EOF (exec/src/lib.rs read_to_end)."""
    fd, path = tempfile.mkstemp(prefix="pycodex-prompt-")
    try:
        os.write(fd, (prompt or "").encode())
    finally:
        os.close(fd)
    rfd = os.open(path, os.O_RDONLY)
    os.unlink(path)
    return rfd

#: codex's TUI onboarding folder-trust question (tui/src/onboarding/trust_directory.rs), matched
#: WHITESPACE-INSENSITIVELY on the ANSI-stripped tail: the TUI positions the cursor between glyphs,
#: so the raw stream interleaves escapes and the stripped text loses its spaces
#: ("Doyoutrustthecontentsofthisdirectory?"). A contiguous literal match on raw bytes never fires.
_TRUST_PROMPT = re.compile(r"trustthecontentsofthis", re.IGNORECASE)
_ANSWER_SCAN_TAIL = 8192          # the prompt is at startup; bound the rescan on a growing buffer


class CodexPopen(WirePtyPopen[BaseProcess]):
    """``WirePtyPopen`` for a codex run — the same PTY launch flavour as agy's, so ``codex exec``
    and ``agy --print`` (and the two TUI modes) sit on identical machinery. Reads its config off
    ``process_obj`` (``_prompt``/``_workdir``/``_capture``/``_model``/``_extra_flags``/``_codex_bin``/
    ``_extra_env``/``_persistent``/``_session_id``/``_continue_latest``); the PTY mechanics come from
    ``WirePtyPopen`` and the boot channel + lifecycle from ``WirePopen``.

    Two deliberate deviations from the PTY defaults, both because codex is not agy:
      * one-shot (``codex exec``) sets ``_stdin_devnull`` — exec blocks reading stdin, and unlike the
        TUI there is nothing to type into it. It still gets the pty slave on stdout/stderr.
      * the death sentinel stays ``os.pidfd_open``: codex spawns tool grandchildren (shells,
        apply_patch) that inherit the pty slave, so the PTY master can outlive codex itself."""
    method = "codex"

    def _resolve_launch(self, process_obj: BaseProcess) -> tuple[list[str], dict[str, str], str]:
        process = cast(_CodexLaunch, process_obj)
        workdir = process._workdir or os.getcwd()
        self._workspace = workdir
        capture = process._capture or os.path.join(workdir, "codex-capture.jsonl")
        self._capture_path = capture
        persistent = process._persistent
        via_stdin = bool(process._prompt_via_stdin) and not persistent
        # exec blocks on stdin; the TUI needs the slave there to be typed into (write/send_line).
        # With a stdin prompt, fd 0 is the unlinked prompt file instead of /dev/null.
        if via_stdin:
            self._stdin_fd = _prompt_stdin_fd(process._prompt)
            self._stdin_devnull = False
        else:
            self._stdin_devnull = not persistent
        self._trust_answered = not persistent   # only the TUI shows the trust prompt
        # linked_env sets WIRE_ENABLE/WIRE_MODULE/WIRE_CAPTURE/PYTHONHOME/CODEX_HOME
        # (+ extra_env, e.g. OPENAI_API_KEY); the base (WirePopen._launch) adds WIRE_MP_BOOT_FD.
        env = linked_env(capture, extra_env=process._extra_env,
                               codex_home=process._codex_home)
        argv = codex_argv(process._prompt, workdir, model=process._model,
                          extra_flags=process._extra_flags, codex_bin=process._codex_bin,
                          persistent=persistent,
                          session_id=process._session_id,
                          continue_latest=process._continue_latest,
                          prompt_via_stdin=via_stdin)
        return argv, env, workdir

    def _answer(self) -> None:
        """Answer codex's TUI onboarding prompt. The interactive path opens with a folder-trust
        question ("Do you trust the contents of this directory?",
        tui/src/onboarding/trust_directory.rs) whose FIRST choice is "Yes, continue" — so Enter
        selects it, exactly like agy's folder-trust menu. Without this the TUI blocks forever and
        no turn is ever decoded. One-shot `codex exec` never shows it, and answering twice is
        harmless, so the flag just avoids re-sending on every read.

        Deliberately NOT auto-answered: the rate-limit dialog codex raises on a usage limit
        ("Switch to <cheaper model> / Keep current model / Press enter to confirm"). Which model
        a run uses is the caller's decision, so silently picking one would be wrong — a Session
        that hits a usage limit stalls there visibly instead."""
        if self._trust_answered:
            return
        tail = strip_ansi(bytes(self.raw[-_ANSWER_SCAN_TAIL:]))
        if _TRUST_PROMPT.search(re.sub(r"\s+", "", tail)):
            try:
                os.write(self.fd, b"\r")
                self._trust_answered = True
            except OSError:
                pass

    def _make_sentinel(self) -> int | None:
        """codex spawns tool grandchildren that inherit the pty slave, so the master can stay open
        after codex itself exits — track the process instead. Falls back to no sentinel (the drain
        polls ``reap()``) on a kernel without pidfd."""
        try:
            return os.pidfd_open(self.pid)
        except (AttributeError, OSError):
            return None


class CodexProcess(WirePtyProcess[CodexPopen]):
    """``WirePtyProcess`` handle for a codex run — the codex twin of ``AgyProcess``, on the same PTY
    machinery. The caller creates the result ``SimpleQueue`` and passes it via
    ``args=(q, ("codex_turn",), max_wait)``; the default target
    (``wirecap.decode.mp_child.stream_turns``) streams the decoded turns home, and the caller drains
    with ``service_pty(timeout, [q._reader])`` + ``q.get()``.

    ``persistent=False`` is ``codex exec`` — the counterpart of ``agy --print``. ``persistent=True``
    is the interactive TUI (drive it with ``.send()``/``.send_line()``), the counterpart of
    ``agy --prompt-interactive``. ``session_id`` / ``continue_latest`` resume a stored session, like
    agy's ``conversation_id`` / ``continue_latest``.

    ``service_pty``/``last_output``/``transcript``/``write``/``send_line``/``send``/``workspace`` are
    inherited from ``WirePtyProcess``; ``reap``/``close`` from ``WireProcess``."""

    @staticmethod
    def _Popen(process_obj: BaseProcess) -> CodexPopen:
        return CodexPopen(process_obj)

    def __init__(self, prompt: str | None = None, target: Target | None = None,
                 name: str | None = None, args: tuple[object, ...] = (),
                 kwargs: Mapping[str, object] | None = None, *, workdir: str | None = None,
                 capture: str | None = None, model: str | None = None,
                 extra_flags: Sequence[str] | None = None, codex_bin: str | None = None,
                 extra_env: Mapping[str, str] | None = None, persistent: bool = False,
                 session_id: str | None = None, continue_latest: bool = False,
                 codex_home: str | None = None, prompt_via_stdin: bool = False,
                 echo: bool = False, daemon: bool | None = None) -> None:
        super().__init__(target=target, name=name, args=args, kwargs=kwargs, daemon=daemon)
        self._prompt = prompt
        self._workdir = workdir
        self._capture = capture
        self._model = model
        self._extra_flags = extra_flags
        self._codex_bin = codex_bin
        self._extra_env = extra_env
        self._persistent = persistent          # interactive TUI (drive via .send()); else codex exec
        self._session_id = session_id          # resume a stored session (codex [exec] resume <id>)
        self._continue_latest = continue_latest  # resume the newest (codex [exec] resume --last)
        self._codex_home = codex_home          # first-class CODEX_HOME scoping (auth/sessions)
        self._prompt_via_stdin = prompt_via_stdin  # deliver the prompt on fd 0 (`codex exec -`)
        self._echo = echo                      # mirror codex's PTY output to our stdout (debug)
