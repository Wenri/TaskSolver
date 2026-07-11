"""PtyPopen — launch agy under a PTY as a multiprocessing spawn child, and BE the PTY handler.

`agy` inspects its controlling terminal and refuses to behave without a real TTY, so we fork it
under a pty; it also blocks on terminal-capability queries + the folder-trust menu until answered.
This class owns that fork, the winsize, and the terminal-query auto-reply. The generic spawn-child
machinery (the boot channel, fd inheritance, lifecycle) lives in the shared
`wirecap.runtime.process.WirePopen` base; PtyPopen just fills the launch hooks (`_resolve_launch`
builds agy's instrumented argv/env via PT_INTERP+--preload; `_spawn_child` does the `pty.fork`;
`_interrupt` Ctrl-Cs the TUI on close) and adds the terminal drain (`_service`, auto-answer, the
transcript byproduct).

Every launch is instrumented (the shim is injected via agy's PT_INTERP + --preload — never
LD_PRELOAD, which leaks into agy's children — plus capture on the pinned vendor/agy). Like
`popen_spawn_posix`, this Popen owns the fork + fd inheritance but NOT the result queue: the caller
(client.py) creates the SimpleQueue, passes it as a target arg, and drains it via `_service` — which
services the PTY (drain + answer queries) while waiting on the caller-supplied reader, so no
background thread is needed. The PTY transcript is kept on `self.raw` (via `transcript`) as a
diagnostic byproduct. See `wirecap/decode/mp_child.py` (child side) + `wirecap/runtime/process.py`.

`AgyProcess` (pyagy/agyprocess.py) is the user-facing `SpawnProcess` handle; this is its `_Popen`.
"""
import os
import pty
import time

import multiprocessing.connection as _conn

from wirecap.runtime.process import WirePopen

from . import conversations as _conv
from ._env import _vendored, instrumented_env, preload_argv
from ._term import answer_queries, answer_trust, strip_ansi
from .conversations import ensure_git_workspace

# the pinned agy whose build-id matches the shim: bundled pyagy/vendor/agy (wheel) or the
# sibling antigravity/vendor/agy (checkout) — never an external path.
_VENDOR_AGY = _vendored("vendor/agy", "../vendor/agy")


class PtyPopen(WirePopen):
    """The `WirePopen` for an agy run: execs agy under a PTY and owns the PTY (fork/pump/read/answer).
    Constructed by `AgyProcess._Popen(process_obj)`; reads its config off `process_obj` (`_agy_bin`,
    `_agy_args`, `_workdir`, `_capture`, `_data_dir`, `_trust`, `_extra_env`, `_echo`). The generic
    boot-channel/lifecycle is inherited from `WirePopen`."""
    method = "agy"
    _WINSIZE = (50, 200)

    # --- launch hooks (fill the WirePopen base) ------------------------------
    def _resolve_launch(self, process_obj):
        """Build agy's instrumented argv + env (the base adds WIRE_MP_BOOT_FD). Records the run's
        resolved workspace/capture/home for AgyProcess's accessors."""
        # instrumentation needs the build-id-matched binary: an explicit programmatic agy_bin
        # (tests inject one), else the packaged agy (_VENDOR_AGY — bundled or sibling, never external).
        agy = getattr(process_obj, "_agy_bin", None) or _VENDOR_AGY
        workdir = ensure_git_workspace(getattr(process_obj, "_workdir", None))
        self._workspace = workdir                            # resolved workspace (AgyProcess.workspace)
        capture = getattr(process_obj, "_capture", None) or os.path.join(workdir, "agy-capture.jsonl")
        self._capture_path = capture        # for AgyProcess.conversation_id (conversation_id event)
        self._home, env_ovr = _conv.scope_for_run(
            workdir, getattr(process_obj, "_data_dir", None),
            trust=getattr(process_obj, "_trust", True))     # repo-scoped store + workspace trust
        # caller overlays (shim knobs / rewrite) + scoped-HOME override. The boot pipe fd is added by
        # the base (WirePopen._launch) — the worker channel it owns.
        extra = {**(getattr(process_obj, "_extra_env", None) or {}), **env_ovr}
        env = instrumented_env(capture=capture, extra_env=extra)
        agy_args = getattr(process_obj, "_agy_args", None) or ["--print", "agy-mp"]
        # Inject the shim via agy's PT_INTERP + --preload (per-exec) rather than LD_PRELOAD, which
        # every child agy spawns would inherit and needlessly load the shim into. An explicit empty
        # LD_PRELOAD in extra_env opts out (an uninstrumented baseline — e.g. test auth probes).
        if env.pop("LD_PRELOAD", None) == "":
            argv = [agy, *agy_args]
        else:
            argv = preload_argv(agy, agy_args, env=env)
        return argv, env, workdir

    def _spawn_child(self, argv, workdir, env, process_obj):
        """Init the PTY state, fork agy under a pty (inheriting the now-inheritable queue fds, boot_r,
        and tracker_fd), and set the PTY master as the death sentinel."""
        self.raw = bytearray()          # every byte read from the PTY (transcript byproduct)
        self.status = None              # agy's raw exit status once reaped (AgyProcess.exit_status)
        self._qpos = 0                  # terminal-query scan position
        self._trust_answered = False
        self._echo = getattr(process_obj, "_echo", False)   # mirror agy's PTY output to our stdout
        self._last_output = time.time() # last PTY write (turn-boundary idle for .ask())
        self._pty_dead = False          # set once the master EOFs → _service drops it
        self._snap = _conv.snapshot(home=self._home)   # pre-launch store snapshot → conversation_id
        self._spawn_pty(argv, workdir, env)            # pty.fork + execve(agy) → self.pid, self.fd
        self.sentinel = self.fd                        # PTY master EOFs on agy death (wait(timeout))

    def _interrupt(self):
        """WirePopen.close hook: Ctrl-C twice to break agy out of its TUI before SIGTERM."""
        for _ in range(2):
            try:
                self.write(b"\x03")
                time.sleep(0.2)
            except OSError:
                break

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

    def _service(self, timeout, readers):
        """Drain agy's PTY (+ auto-answer, tracking `_last_output`) while waiting up to `timeout` s
        for data on any of `readers` (the caller's result-queue read end(s)); return True once one is
        ready. Replaces the old background pump thread — the caller drains in the same wait it uses to
        read results, so agy's PTY stays drained without a separate thread. The Popen owns no queue,
        so the reader(s) are passed in. `_conn.wait` watches the reader(s) and the raw PTY fd
        together; once the master EOFs it is dropped from the wait set (no busy-spin)."""
        end = time.time() + timeout
        while True:
            watch = list(readers) if self._pty_dead else [*readers, self.fd]
            ready = _conn.wait(watch, max(0.0, end - time.time()))
            if not self._pty_dead and self.fd in ready:
                if self._read_available():
                    self._last_output = time.time()
                else:
                    self._pty_dead = True          # master EOF/closed — stop watching it
            if any(r in ready for r in readers):
                return True
            if time.time() >= end:
                return False


def service_many(popens, readers, timeout):
    """PTY-multiplex primitive for draining several agy PTYs while waiting on their result readers
    in one `_conn.wait`. `popens` and `readers` are parallel lists — one live `(PtyPopen, reader)`
    pair each. Does ONE wait: drains every PTY that is readable (+ auto-answers, marking `_pty_dead`
    on EOF) and returns the sublist of `readers` that are ready. No busy-spin; the caller loops and
    owns the queues + collection policy (mirrors single-proc `_service`, but across N PTYs). The
    Popens own no queues — the reader(s) come from the caller."""
    watch = {}
    for pop, reader in zip(popens, readers):
        watch[reader] = None                   # a result reader (Connection)
        if not pop._pty_dead:
            watch[pop.fd] = pop                # PTY master (int fd) → its Popen; drop once it EOFs
    if not watch:
        return []
    ready = _conn.wait(list(watch), timeout)
    ready_readers = []
    for r in ready:
        pop = watch[r]
        if pop is None:                        # result reader is ready
            ready_readers.append(r)
        elif not pop._read_available():        # PTY readable: drain (+ auto-answer); EOF → dead
            pop._pty_dead = True
    return ready_readers
