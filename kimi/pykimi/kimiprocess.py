"""KimiProcess / KimiPopen — kimi-code driven as a wirecap mp-child, streaming decoded
``kimi_turn``s home over a caller-owned ``SimpleQueue``.

The kimi sibling of pycodex's CodexProcess/CodexPopen, on the shared ``wirecap.runtime.pty``
bases — so ``kimi -p``, ``codex exec`` and ``agy --print`` run on identical machinery. Two
codex-matching deviations from the PTY defaults, for the same reasons:

  * print mode points the child's stdin at ``/dev/null`` — there is nothing to type into it
    (the prompt rides ``-p`` on the argv), and a PTY-slave stdin would leave the CLI thinking
    a user is attached;
  * the death sentinel is ``os.pidfd_open`` rather than the PTY master — kimi-code spawns MCP
    stdio servers and node-pty shell grandchildren that inherit the slave, so the master can
    outlive the CLI itself.

The bridge (loaded by the vendored wiretap patch via ``$WIRE_NODE_ADDON``) runs
``wirecap.decode.mp_child``, whose ``stream_turns`` target ``.put``s ``kimi_turn``s over the
queue; the durable ``WIRE_CAPTURE`` JSONL stays authoritative for the returned turns (client.py).
"""
from __future__ import annotations

import os
import re
import time

from collections.abc import Mapping, Sequence
from multiprocessing.process import BaseProcess
from typing import Protocol, cast

from wirecap.runtime.pty import WirePtyPopen, WirePtyProcess, strip_ansi
from wirecap.runtime.process import Target

from ._env import linked_env, kimi_argv


class _KimiLaunch(Protocol):
    _workdir: str | None
    _capture: str | None
    _prompt: str | None
    _model: str | None
    _extra_flags: Sequence[str] | None
    _kimi_bin: str | None
    _extra_env: Mapping[str, str] | None
    _persistent: bool
    _session_id: str | None
    _continue_latest: bool
    _kimi_home: str | None

#: kimi-code's pre-session trust gate (tui/components/dialogs/trust-prompt.ts):
#: first choice is "Trust this folder", so Enter selects it. Matched
#: whitespace-insensitively on the ANSI-stripped tail — the TUI positions the
#: cursor between glyphs, so the stripped text loses its spaces (same reason
#: as codex's trust matcher).
_TRUST_PROMPT = re.compile(r"trustthisfolder", re.IGNORECASE)
_ANSWER_SCAN_TAIL = 8192   # the dialog appears at startup; bound the rescan


class KimiPopen(WirePtyPopen[BaseProcess]):
    """``WirePtyPopen`` for a kimi-code run. Reads its config off ``process_obj``
    (``_prompt``/``_workdir``/``_capture``/``_model``/``_extra_flags``/``_kimi_bin``/``_extra_env``/
    ``_kimi_home``/``_session_id``/``_continue_latest``/``_persistent``); the PTY mechanics come
    from ``WirePtyPopen`` and the boot channel + lifecycle from ``WirePopen``. Print mode never
    shows an interactive prompt (approvals come from the run's config/permission rules); shell
    mode (``_persistent``) can open with the folder-trust dialog, which ``_answer`` accepts —
    ``pykimi.config.trust_workspace`` pre-seeds the store so it normally never renders at all."""
    method = "kimi"
    _stdin_devnull = True    # print-mode default; _resolve_launch overrides per launch

    def _resolve_launch(self, process_obj: BaseProcess) -> tuple[list[str], dict[str, str], str]:
        process = cast(_KimiLaunch, process_obj)
        workdir = process._workdir or os.getcwd()
        self._workspace = workdir
        capture = process._capture or os.path.join(workdir, "kimi-capture.jsonl")
        self._capture_path = capture
        persistent = process._persistent
        # print mode: nothing is typed, -p carries the prompt, and a PTY-slave
        # stdin would leave the CLI thinking a user is attached. Shell mode IS
        # the attached user — its stdin must stay on the slave to be typed into.
        self._stdin_devnull = not persistent
        self._trust_answered = not persistent   # only the TUI shows the trust dialog
        # linked_env sets WIRE_ENABLE/WIRE_MODULE/WIRE_CAPTURE/WIRE_NODE_ADDON/PYTHONHOME/
        # KIMI_CODE_HOME/KIMI_MODEL_NAME (+ extra_env); the base (WirePopen._launch) adds
        # WIRE_MP_BOOT_FD.
        env = linked_env(capture, extra_env=process._extra_env,
                               kimi_home=process._kimi_home,
                               model=process._model)
        argv = kimi_argv(process._prompt, extra_flags=process._extra_flags,
                         kimi_bin=process._kimi_bin,
                         session_id=process._session_id,
                         continue_latest=process._continue_latest,
                         persistent=persistent)
        return argv, env, workdir

    def _answer(self) -> None:
        """Accept the folder-trust dialog if it renders (first option = "Trust this
        folder", Enter selects it). Belt to ``trust_workspace``'s braces: the seeded
        store record makes the dialog not appear; this catches a run whose home was
        not seeded. Nothing else is auto-answered — permission prompts are governed
        by the run's permission mode, and picking models/plans for the user would
        be wrong."""
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
        """kimi-code's MCP/shell grandchildren inherit the pty slave, so the master can stay open
        after the CLI itself exits — track the process instead. Falls back to no sentinel (the
        drain polls ``reap()``) on a kernel without pidfd."""
        try:
            return os.pidfd_open(self.pid)
        except (AttributeError, OSError):
            return None


class KimiProcess(WirePtyProcess[KimiPopen]):
    """``WirePtyProcess`` handle for a kimi-code run — the kimi twin of ``CodexProcess``, on the
    same PTY machinery. The caller creates the result ``SimpleQueue`` and passes it via
    ``args=(q, ("kimi_turn",), max_wait)``; the default target
    (``wirecap.decode.mp_child.stream_turns``) streams the decoded turns home, and the caller
    drains with ``service_pty(timeout, [q._reader])`` + ``q.get()``.

    ``persistent=False`` is one-shot print mode (``kimi -p``). ``persistent=True`` is the
    interactive shell UI, driven turn-by-turn via :meth:`submit` — used by ``pykimi.Session``;
    the cross-run resume story rides the native store via ``session_id``/``continue_latest``
    in both modes."""

    @staticmethod
    def _Popen(process_obj: BaseProcess) -> KimiPopen:
        return KimiPopen(process_obj)

    def __init__(self, prompt: str | None = None, target: Target | None = None,
                 name: str | None = None, args: tuple[object, ...] = (),
                 kwargs: Mapping[str, object] | None = None, *, workdir: str | None = None,
                 capture: str | None = None, model: str | None = None,
                 extra_flags: Sequence[str] | None = None, kimi_bin: str | None = None,
                 extra_env: Mapping[str, str] | None = None, persistent: bool = False,
                 session_id: str | None = None, continue_latest: bool = False,
                 kimi_home: str | None = None,
                 echo: bool = False, daemon: bool | None = None) -> None:
        super().__init__(target=target, name=name, args=args, kwargs=kwargs, daemon=daemon)
        self._prompt = prompt
        self._workdir = workdir
        self._capture = capture
        self._model = model                    # -> KIMI_MODEL_NAME (env-family model definition)
        self._extra_flags = extra_flags
        self._kimi_bin = kimi_bin              # test stub substitute for `node main.mjs`
        self._extra_env = extra_env
        self._persistent = persistent          # interactive shell UI (drive via .submit())
        self._session_id = session_id          # resume a stored session (-S <id>)
        self._continue_latest = continue_latest  # resume the newest for the workdir (--continue)
        self._kimi_home = kimi_home            # first-class KIMI_CODE_HOME scoping (config/sessions)
        self._echo = echo                      # mirror the CLI's PTY output to our stdout (debug)

    def submit(self, prompt: str) -> None:
        """Type + submit one turn into the shell UI.

        Multi-line prompts are wrapped in **bracketed paste** (``ESC[200~ … ESC[201~``)
        with the submitting CR sent separately: kimi's editor treats a pasted block as
        one insertion, whereas typed-out newlines submit at the first ``\\n`` — a
        multi-line prompt sent via plain ``send_line`` would fire N partial turns
        (editor.ts handles paste vs. Enter distinctly). Single-line prompts go the
        plain route. Resets ``last_output`` either way (the WireSession contract)."""
        text = prompt
        if "\n" in text:
            self.write(b"\x1b[200~" + text.encode() + b"\x1b[201~")
            self.write(b"\r")
            self.last_output = time.time()
        else:
            super().submit(text)
