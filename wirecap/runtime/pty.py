"""PTY transport for linked CLIs: fork, terminal replies, transcript and queue service.

Providers supply launch configuration and terminal-specific replies. The PTY must
be drained while waiting for results so child output cannot block. Its master fd
signals closure of every slave; providers with surviving tool children use a pidfd
for process-death detection, or poll reap() when pidfds are unavailable.
"""
from __future__ import annotations

import os
import pty
import re
import time

import multiprocessing.connection as _conn
from collections.abc import Sequence
from multiprocessing import resource_tracker
from multiprocessing.process import BaseProcess
from typing import Protocol, cast

from .process import WirePopen, WirePopenHandle, WireProcess


class WirePtyHandle(WirePopenHandle, Protocol):
    fd: int
    _last_output: float
    _pty_dead: bool
    @property
    def _workspace(self) -> str | None: ...
    @property
    def death_sentinel(self) -> int | None: ...
    @property
    def transcript(self) -> str: ...
    def write(self, data: bytes) -> None: ...
    def send_line(self, text: str) -> None: ...
    def _service(self, timeout: float, readers: Sequence[_conn.Connection | int]) -> bool: ...
    def _read_available(self) -> bytes: ...


class _ForkIntent(Protocol):
    preserve_fd: bool


def _drain_ready(popen: WirePtyHandle) -> None:
    """Keep the queued PTY tail when a result/death fd ends the caller's wait.

    A ready pidfd or queue EOF can coincide with several short PTY reads.
    Bound the drain so a surviving tool that keeps writing cannot monopolize it.
    """
    remaining = 1024 * 1024
    deadline = time.monotonic() + 0.05
    while not popen._pty_dead and remaining > 0 and time.monotonic() < deadline:
        if not _conn.wait([popen.fd], 0):
            break
        chunk = popen._read_available()
        if not chunk:
            popen._pty_dead = True
            break
        popen._last_output = time.time()
        remaining -= len(chunk)

# OSC/DCS/CSI escapes + stray control chars (keep \t \n \r). This is the union of
# what a TUI CLI emits; anything left after .sub() is human-readable transcript text.
_ANSI = re.compile(
    r"""\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)   # OSC ... BEL/ST
      | \x1b[P^_][^\x1b]*\x1b\\             # DCS/PM/APC ... ST
      | \x1b\[[0-?]*[ -/]*[@-~]             # CSI (params 0x30-0x3F: 0-9 : ; < = > ?)
      | \x1b[@-Z\\-_]                       # 2-byte escapes
      | [\x00-\x08\x0b\x0c\x0e-\x1f]        # stray control chars (keep \t \n \r)
    """,
    re.VERBOSE,
)


def strip_ansi(b: str | bytes | bytearray) -> str:
    """Decode (if bytes) and strip ANSI/control sequences → plain transcript text."""
    if isinstance(b, (bytes, bytearray)):
        b = bytes(b).decode("utf-8", "replace")
    return _ANSI.sub("", b)


#: Lines OUR instrumentation writes onto the CLI's stderr, which the pty merges into the transcript.
#: Both providers need them gone before the transcript can serve as a fallback answer — the bridge's
#: own "[wirecap/py] worker ready (… maxcopy=1048576)" banner is otherwise indistinguishable from
#: model output (it will happily satisfy a "reply with a number" parse).
_LOG_MARKERS = ("[antigravity", "[wirecap", "[agy_process]", "[cgt_args]", "gohook", "gomod")


def answer_text(transcript: str) -> str:
    """The clean answer from a (possibly linked) PTY transcript: drop our shim/bridge log
    lines and blank lines, keep the rest."""
    lines = [ln for ln in transcript.splitlines()
             if ln.strip() and not any(m in ln for m in _LOG_MARKERS)]
    return "\n".join(lines).strip()


class WirePtyPopen[ProcessT: BaseProcess = BaseProcess](WirePopen[ProcessT]):
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
    _stdin_fd: int | None = None                     # caller-supplied child stdin (see _spawn_pty)

    _workspace: str | None = None

    # --- launch methods ---------------------------------------------------------
    def _spawn_child(self, argv: list[str], workdir: str, env: dict[str, str],
                     process_obj: ProcessT) -> None:
        """Init PTY state, fork the CLI under a pty (inheriting the now-inheritable queue fds,
        boot_r and tracker_fd), and install the death sentinel."""
        self.raw = bytearray()          # every byte read from the PTY (transcript byproduct)
        self.status = None              # the CLI's raw exit status once reaped
        self._echo = bool(cast(object, getattr(process_obj, "_echo", False)))   # mirror the CLI's output to our stdout
        self._last_output = time.time() # last PTY write (turn-boundary idle detection)
        self._pty_dead = False          # set once the master EOFs → _service drops it
        self._spawn_pty(argv, workdir, env)            # pty.fork + execve → self.pid, self.fd
        self._death_sentinel = self._make_sentinel()
        self.sentinel = self._death_sentinel if self._death_sentinel is not None else self.fd

    @property
    def death_sentinel(self) -> int | None:
        """Process-death fd, or None when the caller must poll reap()."""
        return self._death_sentinel

    def _abort_launch(self) -> None:
        try:
            super()._abort_launch()
        finally:
            if self._stdin_fd is not None:
                os.close(self._stdin_fd)
                self._stdin_fd = None
            # Before the finalizer is installed, a partially spawned PTY is still ours.
            if not self._wire_has_finalizer() and hasattr(self, "fd"):
                os.close(self.fd)

    def _make_sentinel(self) -> int | None:
        """Override: the fd that collection loops can watch for CLI death.

        Default is the PTY master, which EOFs on death — correct for a CLI whose tool children do
        not outlive it. A CLI that spawns grandchildren INHERITING the pty slave should override
        this with ``os.pidfd_open(self.pid)``, which tracks the CLI process itself: the master would
        otherwise stay open as long as any grandchild holds the slave. Note this is independent of
        running under a pty at all — stdio flavour and death detection are separate choices."""
        return self.fd

    def _answer(self) -> None:
        """Override: reply to the CLI's terminal-capability queries / trust prompts. Called after every
        read, with the accumulated bytes on ``self.raw``. Default: nothing to answer."""

    def _teardown_fds(self, boot_w: int) -> tuple[int, ...]:
        """The PTY master joins the finalizer set when it is NOT the sentinel (a pidfd-sentinel
        flavour like codex would otherwise leak one master + pty device per run)."""
        fds = super()._teardown_fds(boot_w)
        if getattr(self, "fd", None) is not None and self.fd != self.sentinel:
            fds = (*fds, self.fd)
        return fds

    # --- PTY mechanics --------------------------------------------------------
    def _spawn_pty(self, argv: list[str], workdir: str, env: dict[str, str]) -> None:
        """`pty.fork()` + `execve(argv[0])` in `workdir`; set the winsize; record pid + master fd."""
        # Python 3.15 closes the resource-tracker fd after raw forks. This child
        # uses the parent's tracker, matching multiprocessing's fork intent.
        intent = cast(_ForkIntent, getattr(resource_tracker, "_fork_intent"))
        intent.preserve_fd = True
        try:
            pid, fd = pty.fork()
        finally:
            intent.preserve_fd = False
        if pid == 0:                          # child
            try:
                os.chdir(workdir)
                if self._stdin_fd is not None:  # caller-supplied stdin (e.g. a prompt file)
                    os.dup2(self._stdin_fd, 0)  # dup2 clears CLOEXEC on 0 → survives execve
                elif self._stdin_devnull:     # one-shot: keep the slave on 1/2, close stdin off it
                    devnull = os.open(os.devnull, os.O_RDONLY)
                    os.dup2(devnull, 0)
                    os.close(devnull)
                os.execve(argv[0], argv, env)
            except Exception as e:            # pragma: no cover
                os.write(2, f"exec failed: {e}\n".encode())
            os._exit(127)
        self.pid, self.fd = pid, fd
        if self._stdin_fd is not None:        # parent keeps no copy; the child owns fd 0 now
            try:
                os.close(self._stdin_fd)
            except OSError:
                pass
            self._stdin_fd = None
        try:
            import fcntl
            import struct
            import termios
            rows, cols = self._WINSIZE
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def _read_available(self) -> bytes:
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
    def transcript(self) -> str:
        """The full ANSI-stripped transcript seen so far."""
        return strip_ansi(bytes(self.raw))

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def send_line(self, text: str) -> None:
        """Type a line and press Enter (CR is what TUIs expect)."""
        self.write(text.encode() + b"\r")

    def _service(self, timeout: float, readers: Sequence[_conn.Connection | int]) -> bool:
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
                _drain_ready(self)
                return True
            if time.time() >= end:
                return False


class WirePtyProcess[PopenT: WirePtyHandle = WirePtyHandle](WireProcess[PopenT]):
    """``WireProcess`` handle for a CLI running under a pty — the Process-level twin of
    ``WirePtyPopen``. Like stock ``Process`` it does NOT own the result channel: the caller passes a
    ``SimpleQueue`` via ``args=(q, ...)`` and drains it with ``service_pty(timeout, [q._reader])`` +
    ``q.get()``, which keeps the pty drained in the same wait it uses to read results (no background
    pump thread, and no risk of the child blocking on a full master buffer).
    ``reap``/``exit_status``/``close`` come from ``WireProcess``."""

    # --- PTY service passthroughs (the caller owns the result queue and drains it via these) ---
    def service_pty(self, timeout: float, readers: Sequence[_conn.Connection | int]) -> bool:
        """Drain the CLI's PTY (+ auto-answer) while waiting up to ``timeout`` s for data on any of
        ``readers`` (the caller's result-queue read end(s)); True once one is ready."""
        return self._wire_popen()._service(timeout, readers)

    @property
    def last_output(self) -> float:
        """Wall-clock of the last PTY write — the turn-boundary idle signal an ask-loop uses to
        detect a settled turn. Settable so the caller can reset it right after submitting a prompt
        (so the idle detector measures from the submit, not the prior turn)."""
        return self._wire_popen()._last_output

    @last_output.setter
    def last_output(self, ts: float) -> None:
        self._wire_popen()._last_output = ts

    # --- PTY: raw input + the transcript byproduct ---
    @property
    def transcript(self) -> str:
        """The full ANSI-stripped PTY transcript seen so far (diagnostics / fallback answer)."""
        return self._wire_popen().transcript

    def write(self, data: bytes) -> None:
        """Write raw bytes to the PTY."""
        self._wire_popen().write(data)

    def send_line(self, text: str) -> None:
        """Type a line + Enter into the PTY."""
        self._wire_popen().send_line(text)

    def submit(self, prompt: str) -> None:
        """Type + submit a prompt into the CLI's interactive TUI.

        The one primitive `wirecap.runtime.session.ask_turn` submits through:
        it types the line AND resets ``last_output``, so the idle clock starts
        at the submit and the reset can never be forgotten by a caller."""
        self._wire_popen().send_line(prompt)
        self._wire_popen()._last_output = time.time()

    #: Historical name for :meth:`submit`.
    send = submit

    @property
    def workspace(self) -> str | None:
        """The resolved git workspace the CLI ran in."""
        return self._popen._workspace if self._popen is not None else None


def service_many(popens: Sequence[WirePtyHandle], readers: Sequence[_conn.Connection],
                 timeout: float) -> list[_conn.Connection]:
    """PTY-multiplex primitive for draining several CLI PTYs while waiting on their result readers
    in one `_conn.wait`. `popens` and `readers` are parallel lists — one live `(WirePtyPopen, reader)`
    pair each. Does ONE wait: drains every PTY that is readable (+ auto-answers, marking `_pty_dead`
    on EOF) and returns the sublist of `readers` that are ready. No busy-spin; the caller loops and
    owns the queues + collection policy (mirrors single-proc `_service`, but across N PTYs). The
    Popens own no queues — the reader(s) come from the caller."""
    watch: dict[_conn.Connection | int, WirePtyHandle | None] = {}
    for pop, reader in zip(popens, readers):
        watch[reader] = None                   # a result reader (Connection)
        if not pop._pty_dead:
            watch[pop.fd] = pop                # PTY master (int fd) → its Popen; drop once it EOFs
    if not watch:
        return []
    ready = _conn.wait(list(watch), timeout)
    ready_readers: list[_conn.Connection] = []
    for r in ready:
        assert isinstance(r, (_conn.Connection, int))
        pop = watch[r]
        if pop is None:                        # result reader is ready
            assert isinstance(r, _conn.Connection)
            ready_readers.append(r)
        elif pop._read_available():
            pop._last_output = time.time()
        else:
            pop._pty_dead = True
    for pop, reader in zip(popens, readers):
        if reader in ready_readers:
            _drain_ready(pop)
    return ready_readers
