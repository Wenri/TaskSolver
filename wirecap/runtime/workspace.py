"""A throwaway git workspace — shared by every wrapper.

Both agy and codex refuse to run outside a git repo (agy always; codex unless
``--skip-git-repo-check``), and both wrappers want a disposable one when the caller
doesn't supply a workspace. One reusable scratch repo is created lazily and reused.
"""
import os
import subprocess
import tempfile

_scratch_ws = None


def ensure_git_workspace(path=None, prefix="wire-ws-") -> str:
    """Return a git workspace path. If ``path`` is given, use it; otherwise create (once) a
    reusable throwaway repo and return it on subsequent calls."""
    global _scratch_ws
    if path:
        return path
    if _scratch_ws and os.path.isdir(os.path.join(_scratch_ws, ".git")):
        return _scratch_ws
    d = tempfile.mkdtemp(prefix=prefix)
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    subprocess.run(["git", "config", "user.email", "wirecap@local"], cwd=d, check=False)
    subprocess.run(["git", "config", "user.name", "wirecap"], cwd=d, check=False)
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("# scratch workspace\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=False)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=False)
    _scratch_ws = d
    return d
