"""WirePtyPopen — the PTY flavour of :class:`wirecap.runtime.process.WirePopen`.

Some agent CLIs inspect their controlling terminal and refuse to behave without a real TTY (agy
does), and block on terminal-capability queries until answered. This module owns everything about
running such a CLI under a pty — the fork, the winsize, reading/echoing the master, the transcript
byproduct, and the drain-while-waiting loop — with NO provider specifics. A provider subclasses it
and fills:

  * ``_resolve_launch(process_obj)``  (from ``WirePopen``) → the instrumented ``argv, env, workdir``
  * ``_answer()``                     → reply to that CLI's terminal prompts (default: no-op)
  * ``_interrupt()``                  (from ``WirePopen``) → break the CLI out of its TUI on close

The PTY master doubles as the death sentinel: it EOFs when the child (and every process holding the
slave) is gone. NOTE that this makes a PTY the WRONG launch flavour for a CLI that spawns tool
grandchildren which inherit the slave — a lingering grandchild keeps the master open after the CLI
itself dies. Those providers should stay on a plain fork with an ``os.pidfd_open`` sentinel (see
``pycodex.codexprocess.CodexPopen``). A PTY also requires that SOMEONE keep draining it — a full
master buffer blocks the child mid-write — which is what ``_service``/``service_many`` are for.

`pyagy._pty.PtyPopen` is the agy subclass. See ``wirecap/decode/mp_child.py`` (child side) +
``wirecap/runtime/process.py`` (the generic spawn/boot-channel base).
"""
import os
import pty
import re
import time

import multiprocessing.connection as _conn

from .process import WirePopen, WireProcess

# OSC/DCS/CSI escapes + stray control chars (keep \t \n \r). This is the union of
# what a TUI CLI emits; anything left after .sub() is human-readable transcript text.
_ANSI = re.compile(
    r"""\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)   # OSC ... BEL/ST
      | \x1b[P^_][^\x1b]*\x1b\\             # DCS/PM/APC ... ST
      | \x1b\[[0-9;?]*[ -/]*[@-~]           # CSI
      | \x1b[@-Z\\-_]                       # 2-byte escapes
      | [\x00-\x08\x0b\x0c\x0e-\x1f]        # stray control chars (keep \t \n \r)
    """,
    re.VERBOSE,
)


def strip_ansi(b) -> str:
    """Decode (if bytes) and strip ANSI/control sequences → plain transcript text."""
    if isinstance(b, (bytes, bytearray)):
        b = bytes(b).decode("utf-8", "replace")
    return _ANSI.sub("", b)


#: Lines OUR instrumentation writes onto the CLI's stderr, which the pty merges into the transcript.
#: Both providers need them gone before the transcript can serve as a fallback answer — the bridge's
#: own "[wirecap/py] worker ready (… maxcopy=1048576)" banner is otherwise indistinguishable from
#: model output (it will happily satisfy a "reply with a number" parse).
_LOG_MARKERS = ("[antigravity", "[wirecap", "[agy_process]", "[cgt_args]", "gohook", "gomod")


def answer_text(transcript) -> str:
    """The clean answer from a (possibly instrumented) PTY transcript: drop our shim/bridge log
    lines and blank lines, keep the rest."""
    lines = [ln for ln in transcript.splitlines()
             if ln.strip() and not any(m in ln for m in _LOG_MARKERS)]
    return "\n".join(lines).strip()


class WirePtyPopen(WirePopen):
    """A ``WirePopen`` that execs its CLI under a pty and owns that pty (fork/read/answer/drain).

    Adds over the base: ``raw`` (every byte read — the transcript byproduct), ``transcript``,
    ``write``/``send_line`` for driving a TUI, and ``_service`` for draining the master while
    waiting on the caller's result-queue reader(s). Owns no queue: like ``popen_spawn_posix`` the
    caller creates the ``SimpleQueue``, passes it as a target arg, and hands the reader(s) in.
    """
    _WINSIZE = (50, 200)
    #: Point the child's stdin at /dev/null instead of the pty slave. Set by a ONE-SHOT launch whose
    #: CLI would otherwise block reading stdin (``codex exec`` does); the child still gets the slave
    #: on stdout/stderr, so it sees a TTY and we get its output. An interactive/TUI launch leaves
    #: this False — the slave on stdin is what ``write``/``send_line`` type into.
    _stdin_devnull = False

    # --- launch hooks ---------------------------------------------------------
    def _spawn_child(self, argv, workdir, env, process_obj):
        """Init PTY state, fork the CLI under a pty (inheriting the now-inheritable queue fds,
        boot_r and tracker_fd), and install the death sentinel."""
        self.raw = bytearray()          # every byte read from the PTY (transcript byproduct)
        self.status = None              # the CLI's raw exit status once reaped
        self._echo = getattr(process_obj, "_echo", False)   # mirror the CLI's output to our stdout
        self._last_output = time.time() # last PTY write (turn-boundary idle detection)
        self._pty_dead = False          # set once the master EOFs → _service drops it
        self._spawn_pty(argv, workdir, env)            # pty.fork + execve → self.pid, self.fd
        self.sentinel = self._make_sentinel()

    def _make_sentinel(self):
        """Hook: the fd that becomes readable when the CLI is gone (``WirePopen.wait`` watches it).

        Default is the PTY master, which EOFs on death — correct for a CLI whose tool children do
        not outlive it. A CLI that spawns grandchildren INHERITING the pty slave should override
        this with ``os.pidfd_open(self.pid)``, which tracks the CLI process itself: the master would
        otherwise stay open as long as any grandchild holds the slave. Note this is independent of
        running under a pty at all — stdio flavour and death detection are separate choices."""
        return self.fd

    def _answer(self):
        """Hook: reply to the CLI's terminal-capability queries / trust prompts. Called after every
        read, with the accumulated bytes on ``self.raw``. Default: nothing to answer."""

    # --- PTY mechanics --------------------------------------------------------
    def _spawn_pty(self, argv, workdir, env):
        """`pty.fork()` + `execve(argv[0])` in `workdir`; set the winsize; record pid + master fd."""
        pid, fd = pty.fork()
        if pid == 0:                          # child
            try:
                os.chdir(workdir)
                if self._stdin_devnull:       # one-shot: keep the slave on 1/2, close stdin off it
                    devnull = os.open(os.devnull, os.O_RDONLY)
                    os.dup2(devnull, 0)       # dup2 clears CLOEXEC on 0 → survives execve
                    os.close(devnull)
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

    def _read_available(self):
        """Read + auto-answer whatever is on the PTY right now (the fd is assumed readable) and
        append it to `raw`. Returns the bytes, or b'' on EOF / a closed master (the CLI is gone)."""
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
        """Drain the CLI's PTY (+ auto-answer, tracking `_last_output`) while waiting up to
        `timeout` s for data on any of `readers` (the caller's result-queue read end(s)); return
        True once one is ready. Replaces a background pump thread — the caller drains in the same
        wait it uses to read results, so the PTY stays drained without a separate thread. The Popen
        owns no queue, so the reader(s) are passed in. `_conn.wait` watches the reader(s) and the raw
        PTY fd together; once the master EOFs it is dropped from the wait set (no busy-spin)."""
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


class WirePtyProcess(WireProcess):
    """``WireProcess`` handle for a CLI running under a pty — the Process-level twin of
    ``WirePtyPopen``. Like stock ``Process`` it does NOT own the result channel: the caller passes a
    ``SimpleQueue`` via ``args=(q, ...)`` and drains it with ``service_pty(timeout, [q._reader])`` +
    ``q.get()``, which keeps the pty drained in the same wait it uses to read results (no background
    pump thread, and no risk of the child blocking on a full master buffer).
    ``reap``/``exit_status``/``close`` come from ``WireProcess``."""

    # --- PTY service passthroughs (the caller owns the result queue and drains it via these) ---
    def service_pty(self, timeout, readers):
        """Drain the CLI's PTY (+ auto-answer) while waiting up to ``timeout`` s for data on any of
        ``readers`` (the caller's result-queue read end(s)); True once one is ready."""
        return self._popen._service(timeout, readers)

    @property
    def last_output(self):
        """Wall-clock of the last PTY write — the turn-boundary idle signal an ask-loop uses to
        detect a settled turn. Settable so the caller can reset it right after submitting a prompt
        (so the idle detector measures from the submit, not the prior turn)."""
        return self._popen._last_output

    @last_output.setter
    def last_output(self, ts):
        self._popen._last_output = ts

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
        """Type + submit a prompt into the CLI's interactive TUI (fire-and-forget)."""
        self._popen.send_line(prompt)
        self._popen._last_output = time.time()

    @property
    def workspace(self):
        """The resolved git workspace the CLI ran in."""
        return getattr(self._popen, "_workspace", None)


def service_many(popens, readers, timeout):
    """PTY-multiplex primitive for draining several CLI PTYs while waiting on their result readers
    in one `_conn.wait`. `popens` and `readers` are parallel lists — one live `(WirePtyPopen, reader)`
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
