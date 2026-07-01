"""Drive the Antigravity `agy` CLI under a PTY.

Two facts force this shape (see antigravity/README.md for the full reverse-engineering):
  * agy needs a real TTY — it inspects the terminal, so we run it under a pty.
  * agy needs a real **git workspace** — an empty dir hangs at startup.

`run_print()` is the one-shot path (`agy --print <prompt>`) used by the TaskSolver
backend: it returns agy's response text cleanly (no TUI). `InteractiveSession`
drives a multi-turn TUI session, answering the terminal-capability queries agy
blocks on, for scripted agentic use.
"""
import os
import pty
import re
import select
import struct
import subprocess
import tempfile
import time

AGY_BIN = os.environ.get("AGY_BIN", os.path.expanduser("~/.local/bin/agy"))

_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[P^_][^\x1b]*\x1b\\"
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1f]"
)


def strip_ansi(b) -> str:
    if isinstance(b, (bytes, bytearray)):
        b = bytes(b).decode("utf-8", "replace")
    return _ANSI.sub("", b)


# Terminal-capability queries agy sends and blocks on; reply like a real terminal.
_QUERIES = [
    (re.compile(rb"\x1b\[\?(\d+)\$p"), lambda m: b"\x1b[?" + m.group(1) + b";0$y"),
    (re.compile(rb"\x1b\[>0?q"),       lambda m: b"\x1bP>|agy-session\x1b\\"),
    (re.compile(rb"\x1b\[\?u"),        lambda m: b"\x1b[?0u"),
    (re.compile(rb"\x1b\[>0?c"),       lambda m: b"\x1b[>0;10;1c"),
    (re.compile(rb"\x1b\[0?c"),        lambda m: b"\x1b[?1;2c"),
    (re.compile(rb"\x1b\[6n"),         lambda m: b"\x1b[50;200R"),
    (re.compile(rb"\x1b\[5n"),         lambda m: b"\x1b[0n"),
]

_scratch_ws = None


def ensure_git_workspace(path=None) -> str:
    """Return a git workspace path (agy refuses to run without one). Creates a
    reusable throwaway repo if none is given."""
    global _scratch_ws
    if path:
        return path
    if _scratch_ws and os.path.isdir(os.path.join(_scratch_ws, ".git")):
        return _scratch_ws
    d = tempfile.mkdtemp(prefix="agy-ws-")
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    subprocess.run(["git", "config", "user.email", "agy@local"], cwd=d, check=False)
    subprocess.run(["git", "config", "user.name", "agy"], cwd=d, check=False)
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("# agy scratch workspace\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=False)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=False)
    _scratch_ws = d
    return d


def _clean_env():
    env = dict(os.environ)
    env["TERM"] = env.get("TERM", "xterm-256color")
    for k in list(env):               # don't inherit the antigravity instrumentation
        if k.startswith("AGY_PROC"):
            env.pop(k, None)
    # keep other preloads (e.g. the WSL1 wsl1-exec.so shim agy runs with); drop only ours
    kept = [p for p in env.get("LD_PRELOAD", "").split(os.pathsep) if p and "antigravity" not in p]
    if kept:
        env["LD_PRELOAD"] = os.pathsep.join(kept)
    else:
        env.pop("LD_PRELOAD", None)
    return env


def _spawn(argv, workdir, env):
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(workdir)
        os.execve(argv[0], argv, env)
        os._exit(127)
    try:
        import fcntl, termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    except Exception:
        pass
    return pid, fd


def run_print(prompt, workdir=None, model=None, timeout=300, skip_permissions=False,
              extra_flags=None):
    """One-shot `agy --print <prompt>`. Returns dict(result, transcript, exit_status)."""
    workdir = ensure_git_workspace(workdir)
    argv = [AGY_BIN, "--print", prompt]
    if model:
        argv += ["--model", model]
    if skip_permissions:
        argv += ["--dangerously-skip-permissions"]
    if extra_flags:
        argv += list(extra_flags)

    pid, fd = _spawn(argv, workdir, _clean_env())
    raw = bytearray()
    qpos = 0
    status = None
    start = time.time()
    while time.time() - start < timeout:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            raw += chunk
            # answer any terminal queries (harmless in print mode)
            while True:
                best = None
                for rx, rep in _QUERIES:
                    m = rx.search(raw, qpos)
                    if m and (best is None or m.start() < best[0].start()):
                        best = (m, rep)
                if not best:
                    qpos = max(qpos, len(raw) - 8)
                    break
                m, rep = best
                try:
                    os.write(fd, rep(m))
                except OSError:
                    break
                qpos = m.end()
        try:
            p, st = os.waitpid(pid, os.WNOHANG)
            if p != 0:
                status = st
                try:
                    while True:
                        c = os.read(fd, 65536)
                        if not c:
                            break
                        raw += c
                except OSError:
                    pass
                break
        except ChildProcessError:
            break
    if status is None:
        try:
            os.kill(pid, 15)
            os.waitpid(pid, 0)
        except Exception:
            pass
    try:
        os.close(fd)
    except OSError:
        pass

    transcript = strip_ansi(raw)
    # The response is the stripped output with surrounding blank lines trimmed.
    result = "\n".join(ln for ln in transcript.splitlines()).strip()
    return {"result": result, "transcript": transcript, "exit_status": status,
            "workspace": workdir}


class InteractiveSession:
    """Multi-turn TUI session (answers terminal queries). See test_scripts/agy_session.py
    for the hook-integrated variant used during capture experiments."""

    def __init__(self, workdir=None, model=None, env=None):
        self.workdir = ensure_git_workspace(workdir)
        self.model = model
        self.env = env or _clean_env()
        self.pid = self.fd = None
        self.raw = bytearray()
        self._qpos = 0

    def start(self, prompt):
        argv = [AGY_BIN, "--prompt-interactive", prompt]
        if self.model:
            argv += ["--model", self.model]
        self.pid, self.fd = _spawn(argv, self.workdir, self.env)
        return self

    def _answer(self):
        while True:
            best = None
            for rx, rep in _QUERIES:
                m = rx.search(self.raw, self._qpos)
                if m and (best is None or m.start() < best[0].start()):
                    best = (m, rep)
            if not best:
                self._qpos = max(self._qpos, len(self.raw) - 8)
                return
            m, rep = best
            try:
                os.write(self.fd, rep(m))
            except OSError:
                return
            self._qpos = m.end()

    def read_until_idle(self, idle=6.0, timeout=180.0):
        start = last = time.time()
        buf = bytearray()
        while time.time() - start < timeout:
            r, _, _ = select.select([self.fd], [], [], min(idle, 1.0))
            if r:
                try:
                    c = os.read(self.fd, 65536)
                except OSError:
                    break
                if not c:
                    break
                buf += c
                self.raw += c
                self._answer()
                last = time.time()
            elif time.time() - last >= idle:
                break
        return strip_ansi(buf)

    def submit(self, text=""):
        os.write(self.fd, text.encode() + b"\r")

    def close(self):
        if not self.pid:
            return
        try:
            os.write(self.fd, b"\x03")
            time.sleep(0.2)
            os.kill(self.pid, 15)
            os.waitpid(self.pid, 0)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
