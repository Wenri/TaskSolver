"""PtyPopen — launch agy under a PTY as a multiprocessing spawn child, and BE the PTY handler.

`agy` inspects its controlling terminal and refuses to behave without a real TTY, so we fork it
under a pty; it also blocks on terminal-capability queries + the folder-trust menu until answered.
This class owns that fork, the winsize, and the terminal-query auto-reply — and, by subclassing
`multiprocessing.popen_fork.Popen`, the process lifecycle too (`poll`/`wait`/`terminate`/`kill`
are inherited and act on agy's pid + the PTY-master sentinel; only `_launch`/`close` are ours).

Every launch is instrumented (LD_PRELOAD shim + capture on the pinned vendor/agy) and always wires
the embedded-worker channel — a result SimpleQueue (native mp sync: a Pipe + SemLocks) + a boot pipe
carrying the pickled target + the resource_tracker fd, inherited across agy's execve. The target
streams agy's decoded answer home; the parent collects it with `AgyProcess.collect()` / `ask()`,
which service the PTY (drain + answer queries) in the same `_service` wait they use to read the
queue, so no background thread is needed. The PTY transcript is kept on `self.raw` (via
`transcript`) as a diagnostic byproduct. See
`agy_process/mp_child.py`.

`AgyProcess` (pyagy/agyprocess.py) is the user-facing `SpawnProcess` handle; this is its `_Popen`.
"""
import os
import pty
import signal
import time

import multiprocessing.connection as _conn
from multiprocessing import (get_context as _get_context, reduction,
                             resource_tracker as _rtracker, spawn as mp_spawn, util)
from multiprocessing.context import set_spawning_popen
from multiprocessing.popen_fork import Popen as _ForkPopen
from multiprocessing.popen_spawn_posix import _DupFd

from . import conversations as _conv
from ._env import ROOT, instrumented_env
from ._term import answer_queries, answer_trust, strip_ansi
from .conversations import ensure_git_workspace

_VENDOR_AGY = os.path.join(ROOT, "vendor", "agy")   # the pinned agy whose build-id matches the shim


def _close_agy(fd, result_q, boot_w):
    """PtyPopen's finalizer callback. Module-level, closing over only fds + the queue (never the
    Popen) so weakref-based GC finalization still fires. Closes: the PTY master fd — which SIGHUPs
    agy, doubling as teardown; the result SimpleQueue's pipe ends (its SemLocks unlink via their own
    resource_tracker Finalize when the queue is GC'd); and the boot pipe's write end — whose EOF is
    the worker's parent-death sentinel, so closing it here (or the OS closing it on our crash) tells
    the in-agy worker we are gone. All idempotent; the finalizer is one-shot."""
    try:
        os.close(fd)
    except OSError:
        pass
    if result_q is not None:
        for c in (result_q._reader, result_q._writer):
            try:
                c.close()
            except Exception:
                pass
    try:
        os.close(boot_w)
    except OSError:
        pass


class PtyPopen(_ForkPopen):
    """The `multiprocessing` Popen for an agy run: execs agy (not python) under a PTY and owns
    both the PTY (fork/pump/read/answer) and the fork-Popen lifecycle. Constructed by
    `AgyProcess._Popen(process_obj)`; reads its config off `process_obj` (`_agy_bin`, `_agy_args`,
    `_workdir`, `_capture`, `_data_dir`, `_trust`, `_extra_env`, `_echo`, `_target`)."""
    method = "agy"
    _WINSIZE = (50, 200)
    DupFd = _DupFd   # a pickled fd detaches to the same number (execve preserves inheritable fds)

    def duplicate_for_child(self, fd):
        """Called via reduction.DupFd while pickling the result SimpleQueue's Connections under
        set_spawning_popen: make each fd survive agy's execve. popen_spawn_posix appends to a passfds
        list handed to spawnv_passfds; we exec directly, so inheritance is by the inheritable flag."""
        os.set_inheritable(fd, True)
        return fd

    # --- PTY mechanics -------------------------------------------------------
    def _spawn_pty(self, argv, workdir, env):
        """`pty.fork()` + `execve(argv[0])` in `workdir`; set the winsize; record pid + master fd."""
        pid, fd = pty.fork()
        if pid == 0:                          # child
            try:
                os.chdir(workdir)
                os.execve(argv[0], argv, env)
            except Exception as e:            # pragma: no cover
                os.write(2, f"exec failed: {e}\n".encode())
            os._exit(127)
        self.pid, self.fd = pid, fd
        try:
            import fcntl
            import struct
            import termios
            rows, cols = self._WINSIZE
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def _answer(self):
        """Reply to agy's terminal-capability queries + the folder-trust menu (else it blocks)."""
        self._qpos = answer_queries(self.raw, self._qpos, lambda b: os.write(self.fd, b))
        if not self._trust_answered:
            self._trust_answered = answer_trust(self.raw, lambda b: os.write(self.fd, b))

    def _read_available(self):
        """Read + auto-answer whatever is on the PTY right now (the fd is assumed readable) and
        append it to `raw`. Returns the bytes, or b'' on EOF / a closed master (agy gone)."""
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:
            return b""
        if chunk:
            self.raw += chunk
            self._answer()
            if self._echo:
                os.write(1, chunk)
        return chunk

    @property
    def transcript(self):
        """The full ANSI-stripped transcript seen so far."""
        return strip_ansi(bytes(self.raw))

    def write(self, data):
        os.write(self.fd, data)

    def send_line(self, text):
        """Type a line and press Enter (CR is what TUIs expect)."""
        self.write(text.encode() + b"\r")

    def exited(self):
        """Reap agy non-blockingly; set `self.status` and return True once it has exited."""
        try:
            pid, st = os.waitpid(self.pid, os.WNOHANG)
            if pid != 0:
                self.status = st
                return True
            return False
        except ChildProcessError:
            return True

    # --- launch + lifecycle (the parts that differ from the stock fork Popen) ---
    def _launch(self, process_obj):
        # instrumentation needs the build-id-matched binary: prefer an explicit agy_bin, then
        # the AGY_BIN env override (tests/run-agy.sh point it at vendor/agy), else the pin.
        agy = getattr(process_obj, "_agy_bin", None) or os.environ.get("AGY_BIN") or _VENDOR_AGY
        workdir = ensure_git_workspace(getattr(process_obj, "_workdir", None))
        self._workspace = workdir                            # resolved workspace (AgyProcess.workspace)
        capture = getattr(process_obj, "_capture", None) or os.path.join(workdir, "agy-capture.jsonl")
        self._capture_path = capture        # for AgyProcess.conversation_id (conversation_id event)
        self._home, env_ovr = _conv.scope_for_run(
            workdir, getattr(process_obj, "_data_dir", None),
            trust=getattr(process_obj, "_trust", True))     # repo-scoped store + workspace trust

        # Result channel: a spawn-context SimpleQueue (a Pipe + two SemLocks — the native mp sync
        # tools) rather than a bare socketpair. put()/get() are synchronous — SimpleQueue has NO
        # feeder thread, so nothing races the neutralized threading._shutdown inside agy. Its SemLocks
        # are named POSIX semaphores registered with the resource_tracker; we hand the child the
        # tracker fd so it re-attaches (like popen_spawn_posix/spawn_main) instead of booting its own.
        ctx = _get_context("spawn")
        self._result_q = ctx.SimpleQueue()
        self._result_reader = self._result_q._reader        # the selectable read end (for _conn.wait)
        tracker_fd = _rtracker.getfd()                       # boots/reuses the parent's tracker
        os.set_inheritable(tracker_fd, True)                 # agy inherits it; the child re-attaches
        boot_r, boot_w = os.pipe()
        os.set_inheritable(boot_r, True)                     # boot_w left CLOEXEC → parent-only sentinel
        # caller overlays (shim knobs / rewrite) + scoped-HOME override + the boot pipe fd (the sole
        # worker-channel signal; its payload carries the tracker fd + the pickled queue + target).
        extra = {**(getattr(process_obj, "_extra_env", None) or {}), **env_ovr,
                 "AGY_MP_BOOT_FD": str(boot_r)}
        env = instrumented_env(capture=capture, extra_env=extra)
        argv = [agy, *(getattr(process_obj, "_agy_args", None) or ["--print", "agy-mp"])]

        # Build the boot payload BEFORE the fork: pickling the queue under set_spawning_popen runs our
        # duplicate_for_child on its pipe fds (set_inheritable) so they survive agy's execve, while its
        # SemLocks pickle by name (the child sem_opens them). The AuthenticationString also refuses to
        # pickle outside the spawning context. Order: tracker fd, queue, prep, process_obj — the child
        # re-attaches the tracker before rebuilding the queue. Payload << 64 KB → the later single
        # write to boot_w is non-blocking even though agy reads it late.
        import io
        prep = mp_spawn.get_preparation_data(process_obj._name)
        buf = io.BytesIO()
        reduction.dump(tracker_fd, buf)            # int; installed onto the child's tracker first
        set_spawning_popen(self)
        try:
            reduction.dump(self._result_q, buf)
            reduction.dump(prep, buf)
            reduction.dump(process_obj, buf)
        finally:
            set_spawning_popen(None)

        # PTY state, then fork agy under the pty (it inherits the now-inheritable queue fds, boot_r,
        # and tracker_fd).
        self.raw = bytearray()          # every byte read from the PTY (transcript byproduct)
        self.status = None              # agy's raw exit status once reaped (AgyProcess.exit_status)
        self._qpos = 0                  # terminal-query scan position
        self._trust_answered = False
        self._echo = getattr(process_obj, "_echo", False)   # mirror agy's PTY output to our stdout
        self._last_output = time.time() # last PTY write (turn-boundary idle for .ask())
        self._pty_dead = False          # set once the master EOFs → _service drops it
        self._snap = _conv.snapshot(home=self._home)   # pre-launch store snapshot → conversation_id
        self._spawn_pty(argv, workdir, env)            # pty.fork + execve(agy)
        self.sentinel = self.fd                        # PTY master EOFs on agy death (wait(timeout))
        # Safety net (mirrors popen_fork's finalizer): via close() or GC of `self` — closes the PTY
        # master (SIGHUPs agy, doubling as teardown), the result queue's pipe ends, and boot_w (the
        # parent-death sentinel). exitpriority None (like popen_fork), so NOT at the atexit sweep.
        self.finalizer = util.Finalize(self, _close_agy, (self.fd, self._result_q, boot_w))

        # Ship the payload to agy over the boot pipe, then keep boot_w open as the sentinel.
        with os.fdopen(boot_w, "wb", closefd=False) as f:
            f.write(buf.getvalue())
        self._result_q._writer.close()     # parent only reads; the child is the sole writer, so the
        #                                    reader now EOFs when agy dies (crash-detection).
        try:
            os.close(boot_r)               # agy has its own inherited copy
        except OSError:
            pass
        # No pump thread: AgyProcess.collect()/poll()/ask() drain the PTY via _service() (waiting on
        # the queue's reader and the PTY together), so agy stays unblocked while we consume results.

    def _service(self, timeout):
        """Worker mode: drain agy's PTY (+ auto-answer, tracking `_last_output`) while waiting up to
        `timeout` s for a result on the queue; return True once the queue's reader has data.
        Replaces the old background pump thread — `AgyProcess.poll()`/`ask()` call this in the same
        wait they use to read results, so agy's PTY stays drained without a separate thread.
        `_conn.wait` watches the queue's read end (a `Connection`) and the raw PTY fd together; once
        the master EOFs it is dropped from the wait set (no busy-spin)."""
        reader = self._result_reader
        end = time.time() + timeout
        while True:
            watch = [reader] if self._pty_dead else [reader, self.fd]
            ready = _conn.wait(watch, max(0.0, end - time.time()))
            if not self._pty_dead and self.fd in ready:
                if self._read_available():
                    self._last_output = time.time()
                else:
                    self._pty_dead = True          # master EOF/closed — stop watching it
            if reader in ready:
                return True
            if time.time() >= end:
                return False

    def close(self, interrupt=False):
        """Tear down: optional Ctrl-C (to break agy's TUI), SIGTERM, reap agy, then run the
        finalizer (closes the PTY master fd + the result queue + boot_w). Safe if agy was already
        reaped."""
        if getattr(self, "pid", None):
            if interrupt:
                for _ in range(2):                       # Ctrl-C twice to break out of the TUI
                    try:
                        self.write(b"\x03")
                        time.sleep(0.2)
                    except OSError:
                        break
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
        if getattr(self, "finalizer", None) is not None:
            self.finalizer()                             # close the PTY master fd (one-shot)
