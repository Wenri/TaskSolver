"""PtyPopen — launch agy under a PTY as a multiprocessing spawn child, and BE the PTY handler.

`agy` inspects its controlling terminal and refuses to behave without a real TTY, so we fork it
under a pty; it also blocks on terminal-capability queries + the folder-trust menu until answered.
This class owns that fork, the winsize, the select-based read loop, and the terminal-query
auto-reply — and, by subclassing `multiprocessing.popen_fork.Popen`, the process lifecycle too
(`poll`/`wait`/`terminate`/`kill` are inherited and act on agy's pid + the PTY-master sentinel;
only `_launch`/`close` are ours). Every launch is instrumented (LD_PRELOAD shim + capture on the
pinned vendor/agy). Two modes, chosen by whether the driving `AgyProcess` set a `target`:

  * plain-CLI (`target=None`) — just run agy; the caller reads the transcript
    (`read_until_exit` one-shot / `read_until_idle` interactive).
  * embedded-worker (`target` set) — also wire a result Pipe + a boot pipe carrying the pickled
    target (`AGY_MP_*`). The PTY is serviced (drained + queries answered) inside `AgyProcess`'s
    poll()/ask() via `_service`, which waits on the Connection and the PTY together — so no
    background thread is needed. See `agy_process/mp_child.py`.

`AgyProcess` (pyagy/agyprocess.py) is the user-facing `SpawnProcess` handle; this is its `_Popen`.
"""
import os
import pty
import select
import signal
import time

import multiprocessing.connection as _conn
from multiprocessing import reduction, spawn as mp_spawn, util
from multiprocessing.context import set_spawning_popen
from multiprocessing.popen_fork import Popen as _ForkPopen

from . import conversations as _conv
from ._env import ROOT, instrumented_env
from ._term import answer_queries, answer_trust, strip_ansi
from .conversations import ensure_git_workspace

_VENDOR_AGY = os.path.join(ROOT, "vendor", "agy")   # the pinned agy whose build-id matches the shim


def _close_agy(fd, conn):
    """PtyPopen's finalizer callback. Module-level, closing over only ``fd``/``conn`` (never the
    Popen) so weakref-based GC finalization still fires: closes the PTY master fd — which SIGHUPs
    agy, doubling as teardown — and the result Connection (``conn`` is None in plain-CLI). Uses
    Connection.close() (not a raw os.close on its fileno) so the object stays in sync; both are
    idempotent and the finalizer is one-shot."""
    try:
        os.close(fd)
    except OSError:
        pass
    if conn is not None:
        conn.close()


class PtyPopen(_ForkPopen):
    """The `multiprocessing` Popen for an agy run: execs agy (not python) under a PTY and owns
    both the PTY (fork/pump/read/answer) and the fork-Popen lifecycle. Constructed by
    `AgyProcess._Popen(process_obj)`; reads its config off `process_obj` (`_agy_bin`, `_agy_args`,
    `_workdir`, `_capture`, `_data_dir`, `_trust`, `_extra_env`, `_echo`, `_target`)."""
    method = "agy"
    _WINSIZE = (50, 200)

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

    def pump(self, timeout):
        """Read whatever is available for up to `timeout` s; append to `raw`; auto-answer; return
        it. Used by the plain-CLI readers (read_until_exit / read_until_idle)."""
        got = bytearray()
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], min(0.3, max(0.0, end - time.time())))
            if not r:
                break
            chunk = self._read_available()
            if not chunk:
                break
            got += chunk
        return got

    def read_until_idle(self, idle=3.0, timeout=120.0):
        """Read until no new output for `idle` s (or agy exits / `timeout`); return this call's
        bytes, ANSI-stripped."""
        start = last = time.time()
        buf = bytearray()
        while time.time() - start < timeout:
            chunk = self.pump(min(idle, 1.0))
            if chunk:
                buf += chunk
                last = time.time()
            elif time.time() - last >= idle:
                break
            if self.pid and self.exited():
                buf += self.pump(0.5)
                break
        return strip_ansi(bytes(buf))

    def read_until_exit(self, timeout=300.0):
        """Read until agy exits (or `timeout`); return the full transcript, ANSI-stripped."""
        start = time.time()
        while time.time() - start < timeout:
            self.pump(0.5)
            if self.exited():
                self.pump(0.5)                # drain trailing output post-exit
                break
        return strip_ansi(bytes(self.raw))

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
        worker = process_obj._target is not None             # embedded-worker mode vs plain-CLI
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
        # caller overlays (shim knobs / rewrite) + the scoped-HOME override, applied last.
        extra = {**(getattr(process_obj, "_extra_env", None) or {}), **env_ovr}

        parent_conn = None
        if worker:
            # embedded-worker channel: a result Pipe + a boot pipe carrying the pickled target,
            # both inherited across agy's execve (CLOEXEC off). See agy_process/mp_child.py.
            parent_conn, child_conn = _conn.Pipe(duplex=True)   # bare socketpair (no semaphore → WSL1-ok)
            boot_r, boot_w = os.pipe()
            os.set_inheritable(child_conn.fileno(), True)
            os.set_inheritable(boot_r, True)
            extra.update({"AGY_MP_MODE": "1",
                          "AGY_MP_CHAN_FD": str(child_conn.fileno()),
                          "AGY_MP_BOOT_FD": str(boot_r)})

        env = instrumented_env(capture=capture, extra_env=extra)
        argv = [agy, *(getattr(process_obj, "_agy_args", None) or ["--print", "agy-mp"])]

        # PTY state, then fork agy under the pty.
        self.raw = bytearray()          # every byte read from the PTY
        self.status = None              # agy's raw exit status once reaped (AgyProcess.exit_status)
        self._qpos = 0                  # terminal-query scan position
        self._trust_answered = False
        self._echo = getattr(process_obj, "_echo", False)   # mirror agy's PTY output to our stdout
        self._parent_conn = parent_conn
        self._snap = _conv.snapshot(home=self._home)   # pre-launch store snapshot → conversation_id
        self._spawn_pty(argv, workdir, env)            # pty.fork + execve(agy); child inherits the fds
        self.sentinel = self.fd                        # PTY master EOFs on agy death (wait(timeout))
        # Safety net (mirrors popen_fork's finalizer): runs via close() or, once nothing references
        # the handle, GC of `self` — closes the PTY master (which SIGHUPs agy, doubling as teardown)
        # + the result Connection. exitpriority is None (like popen_fork), so it does NOT run at the
        # atexit sweep (there the OS reclaims fds and _exit_function terminates agy via _children).
        # One-shot, so it's the sole closer of each. (No pump thread holds `self`, so the GC path
        # works in worker mode too.)
        self.finalizer = util.Finalize(self, _close_agy, (self.fd, parent_conn))

        if not worker:
            return          # plain-CLI: the caller drives the PTY (read_until_exit/idle); no channel/pump

        # --- embedded worker: ship the pickled target in, stream results out over the Connection ---
        self._last_output = time.time()                # last PTY write (turn-boundary idle for .ask())
        self._pty_dead = False                         # set once the master EOFs → _service drops it

        # Pickle (prep_data, process_obj) UNDER set_spawning_popen — BaseProcess.__reduce__ and the
        # AuthenticationString refuse to pickle outside the spawning context (stock _launch does the same).
        import io
        prep = mp_spawn.get_preparation_data(process_obj._name)
        buf = io.BytesIO()
        set_spawning_popen(self)
        try:
            reduction.dump(prep, buf)
            reduction.dump(process_obj, buf)
        finally:
            set_spawning_popen(None)
        # Write the whole payload into the boot pipe now (closing our end = EOF for the in-agy
        # reader). It's a small function+args pickle, << the 64 KB pipe buffer, so this single
        # write lands in the kernel buffer without blocking even though agy reads it late.
        with os.fdopen(boot_w, "wb", closefd=True) as f:
            f.write(buf.getvalue())

        child_conn.close()                                   # parent keeps only parent_conn
        try:
            os.close(boot_r)                                 # child has its own inherited copy
        except OSError:
            pass
        # No pump thread: AgyProcess.poll()/ask() drain the PTY via _service() (waiting on the
        # Connection and the PTY together), so agy stays unblocked while we consume results.

    def _service(self, timeout):
        """Worker mode: drain agy's PTY (+ auto-answer, tracking `_last_output`) while waiting up to
        `timeout` s for a result on the Connection; return True once the Connection has data.
        Replaces the old background pump thread — `AgyProcess.poll()`/`ask()` call this in the same
        wait they use to read results, so agy's PTY stays drained without a separate thread.
        `_conn.wait` watches the Connection and the raw PTY fd together; once the master EOFs it is
        dropped from the wait set (no busy-spin)."""
        conn = self._parent_conn
        end = time.time() + timeout
        while True:
            watch = [conn] if self._pty_dead else [conn, self.fd]
            ready = _conn.wait(watch, max(0.0, end - time.time()))
            if not self._pty_dead and self.fd in ready:
                if self._read_available():
                    self._last_output = time.time()
                else:
                    self._pty_dead = True          # master EOF/closed — stop watching it
            if conn in ready:
                return True
            if time.time() >= end:
                return False

    def close(self, interrupt=False):
        """Tear down: optional Ctrl-C (to break agy's TUI), SIGTERM, reap agy, then run the
        finalizer (closes the PTY master fd + the result Connection). Safe if agy was already
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
