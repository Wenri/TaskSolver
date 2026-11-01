"""Launch a linked CLI as a multiprocessing spawn child.

WirePopen serializes the spawn payload and resource-tracker fd through a boot pipe
that survives exec. The caller owns the result queue; the embedded worker lives in
wirecap.decode.mp_child. Providers supply argv/environment resolution and spawning.
"""
from __future__ import annotations

import io
import os
import signal
import time
from collections.abc import Callable, Mapping
from threading import Lock
from typing import ClassVar, Protocol, cast

from multiprocessing import (reduction, resource_tracker as _rtracker,
                             spawn as mp_spawn, util)
from multiprocessing.context import SpawnProcess, set_spawning_popen
from multiprocessing.popen_spawn_posix import Popen as _SpawnPopen, _DupFd
from multiprocessing.process import BaseProcess


type Target = Callable[..., object]

# Inheritable flags belong to the parent process, including its shared tracker fd.
_FORK_LOCK = Lock()


class WirePopenHandle(Protocol):
    pid: int
    sentinel: int
    @property
    def status(self) -> int | None: ...
    def exited(self) -> bool: ...
    def poll(self, flag: int = os.WNOHANG) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def close(self, interrupt: bool = False, grace: float = 5.0) -> None: ...


def _close_wire(*fds: int | None) -> None:
    """Close owned descriptors; Finalize makes teardown one-shot without retaining Popen.

    The boot writer signals parent death, alongside provider fds such as the PTY
    master and pidfd. The caller owns the result queue separately.
    """
    for fd in fds:
        if fd is None:
            continue
        try:
            os.close(fd)
        except OSError:
            pass


class WirePopen[ProcessT: BaseProcess = BaseProcess](_SpawnPopen):
    """``multiprocessing`` Popen that execs a foreign CLI (not python) as a spawn child, wiring the
    boot channel + result-queue fd inheritance. Subclass fills ``_resolve_launch`` + ``_spawn_child``
    (and optionally ``_interrupt``). Both multiprocessing and capture APIs share
    one reaping path; close also owns process-group and descriptor cleanup."""
    method: ClassVar[str] = "wire"
    status: int | None = None
    pid: int = 0
    returncode: int | None = None
    _reaped = False
    _wire_closed = False
    DupFd = _DupFd   # a pickled fd detaches to the same number (execve preserves inheritable fds)

    def duplicate_for_child(self, fd: int) -> int:
        """Called via ``reduction.DupFd`` while pickling any fd-bearing object under
        ``set_spawning_popen`` (the caller's result SimpleQueue rides ``process_obj``'s args): make
        each fd survive the host's ``execve``. We exec directly, so inheritance is by the inheritable
        flag (not a passfds list as in ``popen_spawn_posix``)."""
        self._inherited_fds.setdefault(fd, os.get_inheritable(fd))
        os.set_inheritable(fd, True)
        return fd

    def poll(self, flag: int = os.WNOHANG) -> int | None:
        """Reap once, keeping multiprocessing and capture exit statuses in sync."""
        if not self._reaped:
            try:
                pid, status = os.waitpid(self.pid, flag)
            except ChildProcessError:
                self._reaped = True
            else:
                if pid:
                    self.status = status
                    self.returncode = os.waitstatus_to_exitcode(status)
                    self._reaped = True
        return self.returncode

    def exited(self) -> bool:
        self.poll()
        return self._reaped

    def wait(self, timeout: float | None = None) -> int | None:
        if timeout is None:
            return self.poll(0)
        # PTY readability can mean output, so it cannot guard a blocking waitpid.
        deadline = time.monotonic() + timeout
        while not self.exited():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.01, remaining))
        return self.returncode

    def _launch(self, process_obj: ProcessT) -> None:
        self._inherited_fds: dict[int, bool] = {}
        boot_r: int | None = None
        boot_w: int | None = None
        try:
            argv, base_env, workdir = self._resolve_launch(process_obj)
            with _FORK_LOCK:
                try:
                    tracker_fd = _rtracker.getfd()
                    assert tracker_fd is not None
                    self.duplicate_for_child(tracker_fd)
                    boot_r, boot_w = os.pipe()
                    self.duplicate_for_child(boot_r)
                    env = dict(base_env, WIRE_MP_BOOT_FD=str(boot_r))

                    # Attach the tracker before unpickling queue SemLocks; pickling marks fds for exec.
                    buf = io.BytesIO()
                    reduction.dump(tracker_fd, buf)
                    set_spawning_popen(self)
                    try:
                        reduction.dump(mp_spawn.get_preparation_data(process_obj.name), buf)
                        reduction.dump(process_obj, buf)
                    finally:
                        set_spawning_popen(None)
                    self._spawn_child(argv, workdir, env, process_obj)
                finally:
                    # Restore flags before another launch observes or changes them.
                    for fd, inheritable in self._inherited_fds.items():
                        try:
                            os.set_inheritable(fd, inheritable)
                        except OSError:
                            pass
                    self._inherited_fds.clear()
            self.finalizer = util.Finalize(self, _close_wire, self._teardown_fds(boot_w))
            boot_writer, boot_w = boot_w, None  # ownership moved to the finalizer
            with os.fdopen(boot_writer, "wb", closefd=False) as stream:
                stream.write(buf.getvalue())
        except BaseException:
            self._abort_launch()
            raise
        finally:
            _close_wire(boot_r)
            _close_wire(boot_w)

    def _wire_has_finalizer(self) -> bool:
        return cast("util.Finalize[None] | None", self.finalizer) is not None

    def _abort_launch(self) -> None:
        self.close(grace=0.1)

    def _resolve_launch(self, process_obj: ProcessT) -> tuple[list[str], dict[str, str], str]:
        """Subclass method → ``(argv, base_env, workdir)``. The base adds ``WIRE_MP_BOOT_FD`` to env."""
        raise NotImplementedError

    def _spawn_child(self, argv: list[str], workdir: str, env: dict[str, str],
                     process_obj: ProcessT) -> None:
        """Subclass method: fork + exec the host in ``workdir`` with ``env``/``argv``; set ``self.pid``
        and ``self.sentinel`` (a fd readable on host death). May init pre/post-fork state."""
        raise NotImplementedError

    def _interrupt(self) -> None:
        """Optional graceful nudge before SIGTERM (agy: Ctrl-C its TUI). Default no-op."""

    def _teardown_fds(self, boot_w: int) -> tuple[int, ...]:
        """Override: the fds the finalizer closes. Base = (sentinel, boot_w); a PTY flavour whose
        sentinel is NOT the master (codex's pidfd) adds the master so it cannot leak."""
        return (self.sentinel, boot_w)

    def close(self, interrupt: bool = False, grace: float = 5.0) -> None:
        """Stop the CLI, reap it, then sweep its process group and close owned fds."""
        if self._wire_closed:
            return
        self._wire_closed = True
        try:
            if self.pid:
                if not self.exited():
                    if interrupt:
                        self._interrupt()
                    self.terminate()
                    self.wait(timeout=grace)
                    if not self._reaped:
                        self.kill()
                        self.wait()
                self._sweep_group(grace)
        finally:
            # multiprocessing initializes this to None before calling _launch.
            finalizer = cast("util.Finalize[None] | None", self.finalizer)
            if finalizer is not None:
                finalizer()

    def _sweep_group(self, grace: float = 5.0) -> None:
        """TERM → bounded wait → KILL the CLI's whole process group (see ``close``)."""
        pid = self.pid
        if not pid or pid == os.getpgrp():
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class WireProcess[PopenT: WirePopenHandle = WirePopenHandle](SpawnProcess):
    """``multiprocessing.Process``-shaped handle for a wirecap-linked CLI run. Like stock
    ``Process`` it does NOT own the result channel: the caller passes a ``SimpleQueue`` via
    ``args=(q,)`` and drains it. Default target = ``wirecap.decode.mp_child.stream_turns``. Subclass
    provides ``_Popen`` (returns the provider's WirePopen) + its config attrs."""

    _popen: PopenT | None = None
    _args: tuple[object, ...]
    _name: str
    _echo: bool = False

    def __init__(self, target: Target | None = None, name: str | None = None,
                 args: tuple[object, ...] = (), kwargs: Mapping[str, object] | None = None,
                 daemon: bool | None = None) -> None:
        if target is None:               # default worker: stream the host's decoded answer home
            from wirecap.decode.mp_child import stream_turns
            target = stream_turns
        super().__init__(group=None, target=target, name=name,
                         args=args, kwargs=(kwargs or {}), daemon=daemon)

    def _wire_popen(self) -> PopenT:
        if self._popen is None:
            raise RuntimeError("process has not started")
        return self._popen

    def reap(self) -> bool:
        """Non-blocking reap so ``exit_status`` is set; True once the host has exited. The caller's
        collect loop calls this when the target signals done / the queue EOFs."""
        return self._wire_popen().exited()

    @property
    def exit_status(self) -> int | None:
        """The host's DECODED exit code — ``os.waitstatus_to_exitcode`` of the raw waitpid status
        (negative for a signal), or None if not yet reaped. Decoded here so both providers report
        the same thing: the raw status made an exit(1) read as 256 on one side and 1 on the other,
        for a value both adapters render into the same user-facing sentence."""
        st = self._popen.status if self._popen is not None else None
        return os.waitstatus_to_exitcode(st) if st is not None else None

    def close(self, *, interrupt: bool = False, grace: float = 5.0) -> None:
        """Stop the host + close the channel. We reap the host ourselves (it owns its lifetime), so
        multiprocessing's active-children set never sees it exit — drop it, else this Process (and
        the result SimpleQueue it carries in ``args``, with its named semaphores) is pinned in
        ``_children`` and leaks until interpreter exit."""
        if self._popen is None:
            return
        self._popen.close(interrupt=interrupt, grace=grace)
        from multiprocessing import process
        children = cast(set[BaseProcess], getattr(process, "_children"))
        children.discard(self)
