"""PTY spawn + read pump shared by every agy driver.

`agy` inspects its controlling terminal and refuses to behave without a real TTY,
so every driver forks it under a pty. This module owns that fork, the winsize, the
select-based read loop, and the terminal-query auto-reply — the three call sites
(`session.run_print`, `session.InteractiveSession`, `test_scripts/agy_session.py`)
differ only in *when* they stop reading (process exit vs. idle gap), so those are
the two `read_*` methods; everything else is shared.
"""
import os
import pty
import select
import signal
import time

from ._term import answer_queries, strip_ansi


class PtyProcess:
    """A child forked under a pty. Read via ``read_until_exit`` (one-shot) or
    ``read_until_idle`` (interactive); both strip ANSI and auto-answer the
    terminal-capability queries agy blocks on."""

    def __init__(self, echo=False, winsize=(50, 200)):
        self.pid = None
        self.fd = None
        self.raw = bytearray()        # every byte read from the pty
        self.status = None            # child exit status once reaped
        self._qpos = 0                # terminal-query scan position
        self.echo = echo
        self.winsize = winsize

    def spawn(self, argv, workdir, env):
        pid, fd = pty.fork()
        if pid == 0:                  # child
            try:
                os.chdir(workdir)
                os.execve(argv[0], argv, env)
            except Exception as e:    # pragma: no cover
                os.write(2, f"exec failed: {e}\n".encode())
            os._exit(127)
        self.pid, self.fd = pid, fd
        try:
            import fcntl
            import struct
            import termios
            rows, cols = self.winsize
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass
        return self

    def _answer(self):
        self._qpos = answer_queries(self.raw, self._qpos, lambda b: os.write(self.fd, b))

    def pump(self, timeout):
        """Read whatever is available for up to ``timeout`` seconds; return it."""
        got = bytearray()
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], min(0.3, max(0.0, end - time.time())))
            if not r:
                break
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            got += chunk
            self.raw += chunk
            self._answer()
            if self.echo:
                os.write(1, chunk)
        return got

    def read_until_idle(self, idle=3.0, timeout=120.0):
        """Read until no new output for ``idle`` s (agent done) or ``timeout``.
        Returns only the bytes read during this call, ANSI-stripped."""
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
        """Read until the child exits or ``timeout``. Returns the full transcript
        (all bytes seen on the pty), ANSI-stripped."""
        start = time.time()
        while time.time() - start < timeout:
            self.pump(0.5)
            if self.exited():
                self.pump(0.5)        # drain trailing output post-exit
                break
        return strip_ansi(bytes(self.raw))

    def write(self, data):
        os.write(self.fd, data)

    def send_line(self, text):
        """Type a line and press Enter (CR is what TUIs expect)."""
        self.write(text.encode() + b"\r")

    def exited(self):
        try:
            pid, st = os.waitpid(self.pid, os.WNOHANG)
            if pid != 0:
                self.status = st
                return True
            return False
        except ChildProcessError:
            return True

    def close(self, interrupt=True):
        if not self.pid:
            return
        if interrupt:
            for _ in range(2):        # Ctrl-C twice to break out of the TUI
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
        try:
            os.close(self.fd)
        except OSError:
            pass
