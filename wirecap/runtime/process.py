"""WirePopen / WireProcess — the shared parent-side machinery for launching a wirecap-instrumented
CLI as a ``multiprocessing.spawn`` child and streaming decoded turns home over a caller-owned
``SimpleQueue``.

Provider-neutral core extracted from pyagy's PtyPopen/AgyProcess. It owns the **boot channel** (a
boot pipe carrying the pickled spawn payload + the resource_tracker fd, inherited across the host's
``execve``) and the process lifecycle, via two hooks a subclass fills in:

  * ``_resolve_launch(process_obj) -> (argv, base_env, workdir)`` — what binary to exec, with which env
  * ``_spawn_child(argv, workdir, env, process_obj)`` — fork+exec the host; set ``self.pid`` + ``self.sentinel``

The child side is :mod:`wirecap.decode.mp_child` (loaded into the host's embedded interpreter). pyagy
adds a PTY (agy is a TUI); pycodex forks plainly (``codex exec`` is a non-TTY one-shot). Like
``popen_spawn_posix`` this Popen owns the fork + fd inheritance but NOT the result queue: the caller
creates the ``SimpleQueue``, passes it as a target arg, and drains it.
"""
import io
import os
import signal

from multiprocessing import (reduction, resource_tracker as _rtracker,
                             spawn as mp_spawn, util)
from multiprocessing.context import SpawnProcess, set_spawning_popen
from multiprocessing.popen_fork import Popen as _ForkPopen
from multiprocessing.popen_spawn_posix import _DupFd


def _close_wire(sentinel_fd, boot_w):
    """Finalizer: close the subclass teardown fd (agy: the PTY master, whose close SIGHUPs agy;
    codex: the pidfd) + the boot pipe's write end (its EOF is the in-host worker's parent-death
    sentinel). Module-level, closing over only fds (never the Popen) so weakref GC still fires.
    Idempotent + one-shot; the caller's result queue is torn down by the caller, not here."""
    for fd in (sentinel_fd, boot_w):
        if fd is None:
            continue
        try:
            os.close(fd)
        except OSError:
            pass


class WirePopen(_ForkPopen):
    """``multiprocessing`` Popen that execs a foreign CLI (not python) as a spawn child, wiring the
    boot channel + result-queue fd inheritance. Subclass fills ``_resolve_launch`` + ``_spawn_child``
    (and optionally ``_interrupt``). ``poll``/``wait``/``terminate``/``kill`` are inherited from
    ``popen_fork`` and act on ``self.pid`` + ``self.sentinel``."""
    method = "wire"
    DupFd = _DupFd   # a pickled fd detaches to the same number (execve preserves inheritable fds)

    def duplicate_for_child(self, fd):
        """Called via ``reduction.DupFd`` while pickling any fd-bearing object under
        ``set_spawning_popen`` (the caller's result SimpleQueue rides ``process_obj``'s args): make
        each fd survive the host's ``execve``. We exec directly, so inheritance is by the inheritable
        flag (not a passfds list as in ``popen_spawn_posix``)."""
        os.set_inheritable(fd, True)
        return fd

    def exited(self):
        """Reap the host non-blockingly; set ``self.status`` (raw waitpid status) + return True once
        it has exited."""
        try:
            pid, st = os.waitpid(self.pid, os.WNOHANG)
            if pid != 0:
                self.status = st
                return True
            return False
        except ChildProcessError:
            return True

    # --- launch (generic; the fork+exec and the env/argv are subclass hooks) ---
    def _launch(self, process_obj):
        argv, base_env, workdir = self._resolve_launch(process_obj)   # subclass: what to exec + env
        # The caller created the result SimpleQueue and put it in process_obj's args, so it rides the
        # process_obj pickle below — this Popen never owns it. We still hand the child the
        # resource_tracker fd so it re-attaches (needed for the queue's SemLocks) instead of booting
        # its own.
        tracker_fd = _rtracker.getfd()
        os.set_inheritable(tracker_fd, True)
        boot_r, boot_w = os.pipe()
        os.set_inheritable(boot_r, True)             # boot_w left non-inheritable → parent-only sentinel
        env = dict(base_env)
        env["WIRE_MP_BOOT_FD"] = str(boot_r)         # the sole worker-channel signal into the host
        # Build the boot payload BEFORE the fork: pickling process_obj under set_spawning_popen runs
        # duplicate_for_child on the queue's pipe fds (carried in process_obj's args) so they survive
        # execve, while its SemLocks pickle by name (the child sem_opens them). Order: tracker fd,
        # prep, process_obj — the child re-attaches the tracker before rebuilding the queue. Payload
        # << 64 KB → the later single write to boot_w is non-blocking even though the host reads late.
        prep = mp_spawn.get_preparation_data(process_obj._name)
        buf = io.BytesIO()
        reduction.dump(tracker_fd, buf)              # int; installed onto the child's tracker first
        set_spawning_popen(self)
        try:
            reduction.dump(prep, buf)
            reduction.dump(process_obj, buf)
        finally:
            set_spawning_popen(None)
        # Subclass forks+execs the host (inheriting the now-inheritable queue fds, boot_r, tracker_fd)
        # and sets self.pid + self.sentinel (a fd that goes readable on host death, for wait()).
        self._spawn_child(argv, workdir, env, process_obj)
        # Safety-net finalizer (mirrors popen_fork): via close() or GC of self — closes the teardown
        # fd + boot_w (the parent-death sentinel). exitpriority None → NOT at the atexit sweep.
        self.finalizer = util.Finalize(self, _close_wire, (self.sentinel, boot_w))
        # Ship the payload to the host over the boot pipe, then keep boot_w open as the sentinel.
        with os.fdopen(boot_w, "wb", closefd=False) as f:
            f.write(buf.getvalue())
        try:
            os.close(boot_r)             # the host has its own inherited copy
        except OSError:
            pass

    def _resolve_launch(self, process_obj):
        """Subclass hook → ``(argv, base_env, workdir)``. The base adds ``WIRE_MP_BOOT_FD`` to env."""
        raise NotImplementedError

    def _spawn_child(self, argv, workdir, env, process_obj):
        """Subclass hook: fork + exec the host in ``workdir`` with ``env``/``argv``; set ``self.pid``
        and ``self.sentinel`` (a fd readable on host death). May init pre/post-fork state."""
        raise NotImplementedError

    def _interrupt(self):
        """Optional graceful nudge before SIGTERM (agy: Ctrl-C its TUI). Default no-op."""

    def close(self, interrupt=False):
        """Tear down: optional ``_interrupt()``, SIGTERM, blocking reap (capturing ``self.status``),
        then the finalizer (closes the teardown fd + boot_w). Safe if the host was already reaped."""
        if getattr(self, "pid", None):
            if interrupt:
                self._interrupt()
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                _, st = os.waitpid(self.pid, 0)
                self.status = st
            except ChildProcessError:
                pass
        if getattr(self, "finalizer", None) is not None:
            self.finalizer()


class WireProcess(SpawnProcess):
    """``multiprocessing.Process``-shaped handle for a wirecap-instrumented CLI run. Like stock
    ``Process`` it does NOT own the result channel: the caller passes a ``SimpleQueue`` via
    ``args=(q,)`` and drains it. Default target = ``wirecap.decode.mp_child.stream_turns``. Subclass
    provides ``_Popen`` (returns the provider's WirePopen) + its config attrs."""

    def __init__(self, target=None, name=None, args=(), kwargs=None, daemon=None):
        if target is None:               # default worker: stream the host's decoded answer home
            from wirecap.decode.mp_child import stream_turns
            target = stream_turns
        super().__init__(group=None, target=target, name=name,
                         args=args, kwargs=(kwargs or {}), daemon=daemon)

    def reap(self):
        """Non-blocking reap so ``exit_status`` is set; True once the host has exited. The caller's
        collect loop calls this when the target signals done / the queue EOFs."""
        return self._popen.exited()

    @property
    def exit_status(self):
        """The host's raw waitpid exit status, or None if not yet reaped."""
        return getattr(self._popen, "status", None)

    def close(self, **popen_kwargs):
        """Stop the host + close the channel. We reap the host ourselves (it owns its lifetime), so
        multiprocessing's active-children set never sees it exit — drop it, else this Process (and
        the result SimpleQueue it carries in ``args``, with its named semaphores) is pinned in
        ``_children`` and leaks until interpreter exit."""
        self._popen.close(**popen_kwargs)
        from multiprocessing.process import _children
        _children.discard(self)
