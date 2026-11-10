"""Optional browser view of an existing terminal manager, served by ttyd."""

from __future__ import annotations

import http.client
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from types import TracebackType
from typing import BinaryIO, Self

from .terminal import TerminalManager

# A packager's ttyd by absolute path (the Nix package pins nixpkgs'); None looks it up on PATH.
TTYD_BIN: str | None = None


class WebTerminal:
    """Attach a browser to managed screens without starting another backend.

    The HTTP server binds only to 127.0.0.1; remote clients can use an SSH
    tunnel. Viewing is read-only unless ``writable=True``. Closing the view
    leaves the backend screens running. With ``screen=...``, the page contains
    only that screen and closes with it. Without a selected screen, the page
    includes all screens and closes with the manager or its last screen.
    ttyd >=1.7.4 is an optional external executable.
    """

    def __init__(self, manager: TerminalManager, *, port: int = 7681,
                 writable: bool = False, ttyd_bin: str | None = None,
                 screen: str | None = None, timeout: float = 10.0, base_path: str = "") -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if not 0 < timeout < float("inf"):
            raise ValueError("timeout must be finite and positive")
        executable = shutil.which(ttyd_bin or TTYD_BIN or "ttyd")
        if executable is None:
            raise FileNotFoundError("web terminal viewing requires ttyd >=1.7.4 on PATH")
        # Earlier ttyd releases accept input by default and use the opposite flag.
        help_result = subprocess.run([executable, "--help"], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     timeout=min(timeout, 3))
        if help_result.returncode or b"--writable" not in help_result.stdout:
            raise RuntimeError("web terminal viewing requires ttyd >=1.7.4 (--writable support)")
        self._process: subprocess.Popen[bytes] | None = None
        self._log: BinaryIO | None = None
        self._close_view: Callable[[], None] | None = None
        self._closed = False
        self.port = port
        self.writable = writable
        # Set when a reverse proxy serves this terminal below a path of its own page.
        self.base_path = base_path
        try:
            selected = manager.screens[screen] if screen is not None else None
            attach, self._close_view = manager._create_view(screen=screen, read_only=not writable)
            command = [executable, "--interface", "127.0.0.1", "--port", str(port),
                       "--check-origin", *(["--writable"] if writable else []),
                       *(["--base-path", base_path] if base_path else []), *attach]
            self._log = tempfile.TemporaryFile()
            env = dict(os.environ)
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            self._process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=self._log,
                stderr=subprocess.STDOUT, env=env, start_new_session=True)
            self._wait_ready(timeout)
            manager._register_cleanup(self.close, screen=selected)
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"Web terminal cleanup also failed: {cleanup_error!r}")
            raise

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _diagnostics(self) -> str:
        if self._log is None:
            return ""
        descriptor = self._log.fileno()
        size = os.fstat(descriptor).st_size
        return os.pread(descriptor, 8192, max(0, size - 8192)).decode("utf-8", "replace")

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive:
                raise RuntimeError(f"ttyd failed to start:\n{self._diagnostics()}")
            # The listening log identifies our listener, including a kernel-assigned port.
            # Merely probing the requested port could accept an unrelated existing server.
            match = re.search(r"Listening on port:\s*(\d+)", self._diagnostics())
            if match:
                self.port = int(match[1])
                connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                        timeout=min(0.2, timeout))
                try:
                    connection.request("GET", self.base_path + "/")
                    response = connection.getresponse()
                    if response.status == 200:
                        return
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
            time.sleep(0.02)
        raise TimeoutError(f"ttyd did not become ready in {timeout:g} seconds:\n{self._diagnostics()}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
        finally:
            try:
                # Destroying this view session also detaches any ttyd-owned clients.
                # The original session and its backend windows remain alive.
                if self._close_view is not None:
                    self._close_view()
                    self._close_view = None
            finally:
                if self._log is not None:
                    self._log.close()
                    self._log = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()
