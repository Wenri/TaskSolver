"""CodexProcess / CodexPopen — codex driven as a wirecap mp-child, streaming decoded ``codex_turn``s
home over a caller-owned ``SimpleQueue``.

The codex sibling of pyagy's AgyProcess/PtyPopen, on the shared ``wirecap.runtime.process`` base.
``codex exec`` is a non-TTY one-shot, so there is NO PTY: CodexPopen forks plainly, points the
child's stdin at ``/dev/null`` (``codex exec`` blocks reading stdin otherwise) and its stdout/stderr
at a per-run logfile (the transcript byproduct), and uses ``os.pidfd_open`` as the death sentinel —
immune to the fd inheritance that makes queue-EOF unreliable once codex spawns tool grandchildren
(shells/apply_patch). The embedded wirecap bridge inside codex runs ``wirecap.decode.mp_child``,
whose ``stream_turns`` target ``.put``s ``codex_turn``s over the queue; the durable ``WIRE_CAPTURE``
JSONL stays authoritative for the returned turns (see client.py).
"""
import os

from wirecap.runtime.process import WirePopen, WireProcess

from ._env import codex_argv, instrumented_env


class CodexPopen(WirePopen):
    """``WirePopen`` for a ``codex exec`` run: plain fork (no PTY) + ``dup2`` stdin=/dev/null,
    stdout/stderr=logfile, + a pidfd death sentinel. Reads its config off ``process_obj``
    (``_prompt``/``_workdir``/``_capture``/``_model``/``_extra_flags``/``_codex_bin``/``_extra_env``);
    the boot channel + lifecycle are inherited from ``WirePopen``."""
    method = "codex"

    def _resolve_launch(self, process_obj):
        workdir = process_obj._workdir
        self._workspace = workdir
        capture = process_obj._capture
        self._capture_path = capture
        # instrumented_env sets WIRE_ENABLE/WIRE_MODULE/WIRE_CAPTURE/PYTHONHOME (+ extra_env, e.g.
        # OPENAI_API_KEY); the base (WirePopen._launch) adds WIRE_MP_BOOT_FD — the worker channel.
        env = instrumented_env(capture, extra_env=process_obj._extra_env)
        argv = codex_argv(process_obj._prompt, workdir, model=process_obj._model,
                          extra_flags=process_obj._extra_flags, codex_bin=process_obj._codex_bin)
        return argv, env, workdir

    def _spawn_child(self, argv, workdir, env, process_obj):
        self.status = None
        self._logpath = os.path.join(workdir, "codex-exec.log")
        # os.open fds are non-inheritable (CLOEXEC) by default; the dup2 targets 0/1/2 are NOT (dup2
        # clears CLOEXEC on the destination), so the child's 0/1/2 survive execve while the originals
        # close on it. The queue fds / boot_r / tracker_fd were made inheritable by WirePopen._launch,
        # so they survive too.
        logfd = os.open(self._logpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        devnull = os.open(os.devnull, os.O_RDONLY)
        pid = os.fork()
        if pid == 0:                          # child
            try:
                os.chdir(workdir)
                os.dup2(devnull, 0)           # codex exec blocks reading stdin otherwise
                os.dup2(logfd, 1)             # stdout + stderr → the transcript logfile
                os.dup2(logfd, 2)
                os.execve(argv[0], argv, env)
            except Exception as e:            # pragma: no cover
                os.write(2, f"exec failed: {e}\n".encode())
            os._exit(127)
        self.pid = pid
        os.close(devnull)                     # parent drops its copies (the child dup2'd them)
        os.close(logfd)
        try:
            self.sentinel = os.pidfd_open(pid)   # readable on codex death — fd-inheritance-immune
        except (AttributeError, OSError):
            self.sentinel = None                 # fallback: the drain polls reap() (no wait() sentinel)


class CodexProcess(WireProcess):
    """``WireProcess`` handle for a ``codex exec`` run. The caller creates the result ``SimpleQueue``
    and passes it via ``args=(q, ("codex_turn",), max_wait)``; the default target
    (``wirecap.decode.mp_child.stream_turns``) streams the decoded turns home. ``exit_status`` is the
    DECODED returncode (matching the old subprocess model); ``transcript`` is the logfile tail."""

    @staticmethod
    def _Popen(process_obj):
        return CodexPopen(process_obj)

    def __init__(self, prompt=None, target=None, name=None, args=(), kwargs=None, *,
                 workdir=None, capture=None, model=None, extra_flags=None,
                 codex_bin=None, extra_env=None, daemon=None):
        super().__init__(target=target, name=name, args=args, kwargs=kwargs, daemon=daemon)
        self._prompt = prompt
        self._workdir = workdir
        self._capture = capture
        self._model = model
        self._extra_flags = extra_flags
        self._codex_bin = codex_bin
        self._extra_env = extra_env

    @property
    def exit_status(self):
        """codex's DECODED exit code (``os.waitstatus_to_exitcode`` of the raw waitpid status), or
        None if not yet reaped — matches the returncode the old subprocess model exposed."""
        st = getattr(self._popen, "status", None)
        return os.waitstatus_to_exitcode(st) if st is not None else None

    @property
    def workspace(self):
        """The git workspace codex ran in."""
        return getattr(self._popen, "_workspace", None)

    @property
    def transcript(self):
        """codex's stdout/stderr for the run (the logfile) — diagnostics + the no-turn fallback."""
        path = getattr(self._popen, "_logpath", None)
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, errors="replace") as f:
                return f.read()
        except OSError:
            return ""
