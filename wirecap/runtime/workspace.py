"""A throwaway git workspace — shared by every wrapper.

Both agy and codex refuse to run outside a git repo (agy always; codex unless
``--skip-git-repo-check``), and both wrappers want a disposable one when the caller
doesn't supply a workspace. One reusable scratch repo is created lazily and reused.
"""
import os
import tempfile

_scratch_ws = None


def ensure_git_workspace(path=None, prefix="wire-ws-") -> str:
    """Return a git workspace path. If ``path`` is given, use it; otherwise create (once) a
    reusable throwaway repo and return it on subsequent calls.

    ``pygit2`` is imported lazily: this module sits on the import chain of the shim's
    embedded interpreter (pyagy.agyprocess -> conversations -> here), which may run under a
    different CPython than the caller's site-packages — a version-locked C extension at
    module level would break it. Only parent-side callers actually create workspaces."""
    global _scratch_ws
    if path:
        return path
    if _scratch_ws and os.path.isdir(os.path.join(_scratch_ws, ".git")):
        return _scratch_ws
    import pygit2
    d = tempfile.mkdtemp(prefix=prefix)
    repo = pygit2.init_repository(d)
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("# scratch workspace\n")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("wirecap", "wirecap@local")
    repo.create_commit("HEAD", sig, sig, "init", tree, [])
    _scratch_ws = d
    return d
