"""Drive the Antigravity `agy` CLI under a PTY.

Two facts force this shape (see antigravity/README.md for the full reverse-engineering):
  * agy needs a real TTY — it inspects the terminal, so we run it under a pty.
  * agy needs a real **git workspace** — an empty dir hangs at startup.

`run_print()` is the one-shot path (`agy --print <prompt>`) used by the TaskSolver
backend: it returns agy's response text cleanly (no TUI). `InteractiveSession`
drives a multi-turn TUI session, answering the terminal-capability queries agy
blocks on, for scripted agentic use.

The PTY fork/pump, ANSI stripping, terminal-query replies, and env wiring live in
the shared `_pty`/`_term`/`_env` modules; this file is just the agy-specific policy
(git workspace, argv assembly, the run_print return dict).
"""
import os
import subprocess
import tempfile

from . import conversations as _conv
from ._env import clean_env
from ._pty import PtyProcess
from ._term import strip_ansi  # re-exported (public API)

AGY_BIN = os.environ.get("AGY_BIN", os.path.expanduser("~/.local/bin/agy"))

_clean_env = clean_env  # backwards-compatible alias

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


def run_print(prompt, workdir=None, model=None, timeout=300, skip_permissions=False,
              extra_flags=None, env=None, conversation_id=None, continue_latest=False,
              data_dir=None, trust=True):
    """One-shot `agy --print <prompt>`. Returns dict(result, transcript, exit_status,
    workspace). ``env`` defaults to a clean (uninstrumented) environment. ``conversation_id``
    resumes a stored conversation (``--conversation=<id>``, works in print mode) and
    ``continue_latest`` resumes the most recent (``--continue``). ``data_dir`` scopes the
    conversation store to a project repo (HOME override + seeded login); ``trust`` pre-trusts
    the workspace (both via :mod:`pyagy.conversations`)."""
    workdir = ensure_git_workspace(workdir)
    home, env_ovr = _conv.scope_for_run(workdir, data_dir, trust=trust)
    argv = [AGY_BIN, "--print", prompt]
    if model:
        argv += ["--model", model]
    if conversation_id:
        argv.append(f"--conversation={conversation_id}")
    elif continue_latest:
        argv.append("--continue")
    if skip_permissions:
        argv += ["--dangerously-skip-permissions"]
    if extra_flags:
        argv += list(extra_flags)

    env = dict(env) if env is not None else clean_env()
    env.update(env_ovr)                     # HOME override for a repo-scoped data dir (if any)
    proc = PtyProcess().spawn(argv, workdir, env)
    transcript = proc.read_until_exit(timeout=timeout)
    proc.close(interrupt=False)

    result = "\n".join(ln for ln in transcript.splitlines()).strip()
    return {"result": result, "transcript": transcript, "exit_status": proc.status,
            "workspace": workdir}


class InteractiveSession:
    """Multi-turn TUI session (answers terminal queries). See test_scripts/agy_session.py
    for the hook-integrated variant used during capture experiments."""

    def __init__(self, workdir=None, model=None, env=None):
        self.workdir = ensure_git_workspace(workdir)
        self.model = model
        self.env = env if env is not None else clean_env()
        self.proc = PtyProcess()

    def start(self, prompt):
        _conv.trust_workspace(self.workdir)      # pre-trust so the folder-trust menu won't block
        argv = [AGY_BIN, "--prompt-interactive", prompt]
        if self.model:
            argv += ["--model", self.model]
        self.proc.spawn(argv, self.workdir, self.env)
        return self

    def read_until_idle(self, idle=6.0, timeout=180.0):
        return self.proc.read_until_idle(idle=idle, timeout=timeout)

    def submit(self, text=""):
        self.proc.send_line(text)

    def close(self):
        self.proc.close(interrupt=True)
