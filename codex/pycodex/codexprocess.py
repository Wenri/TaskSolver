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
import os

from wirecap.runtime.pty import WirePtyPopen, WirePtyProcess

from ._env import codex_argv, instrumented_env


class CodexPopen(WirePtyPopen):
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

    def _resolve_launch(self, process_obj):
        workdir = process_obj._workdir
        self._workspace = workdir
        capture = process_obj._capture
        self._capture_path = capture
        persistent = getattr(process_obj, "_persistent", False)
        # exec blocks on stdin; the TUI needs the slave there to be typed into (write/send_line).
        self._stdin_devnull = not persistent
        # instrumented_env sets WIRE_ENABLE/WIRE_MODULE/WIRE_CAPTURE/PYTHONHOME (+ extra_env, e.g.
        # OPENAI_API_KEY); the base (WirePopen._launch) adds WIRE_MP_BOOT_FD — the worker channel.
        env = instrumented_env(capture, extra_env=process_obj._extra_env)
        argv = codex_argv(process_obj._prompt, workdir, model=process_obj._model,
                          extra_flags=process_obj._extra_flags, codex_bin=process_obj._codex_bin,
                          persistent=persistent,
                          session_id=getattr(process_obj, "_session_id", None),
                          continue_latest=getattr(process_obj, "_continue_latest", False))
        return argv, env, workdir

    def _make_sentinel(self):
        """codex spawns tool grandchildren that inherit the pty slave, so the master can stay open
        after codex itself exits — track the process instead. Falls back to no sentinel (the drain
        polls ``reap()``) on a kernel without pidfd."""
        try:
            return os.pidfd_open(self.pid)
        except (AttributeError, OSError):
            return None


class CodexProcess(WirePtyProcess):
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
    def _Popen(process_obj):
        return CodexPopen(process_obj)

    def __init__(self, prompt=None, target=None, name=None, args=(), kwargs=None, *,
                 workdir=None, capture=None, model=None, extra_flags=None,
                 codex_bin=None, extra_env=None, persistent=False, session_id=None,
                 continue_latest=False, echo=False, daemon=None):
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
        self._echo = echo                      # mirror codex's PTY output to our stdout (debug)
