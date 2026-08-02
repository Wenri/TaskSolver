"""A throwaway git workspace — shared by every wrapper.

Both agy and codex refuse to run outside a git repo (agy always; codex unless
``--skip-git-repo-check``), and both wrappers want a disposable one when the caller
doesn't supply a workspace. One reusable scratch repo is created lazily and reused.

The workspace also carries the run's **standing instructions**: both CLIs discover an
``AGENTS.md`` beside the working directory and apply it for the whole session (agy walks up
from the cwd to the repo root loading ``GEMINI.md``/``AGENTS.md``; codex reads ``AGENTS.md``
plus an ``AGENTS.override.md``). Seeding it here means one hook serves both wrappers and the
per-turn prompt stays short — see ``prompts/`` for the task templates it is meant to carry.
"""
import os
import tempfile

import pygit2

AGENTS_MD = "AGENTS.md"      # the instruction file BOTH agy and codex read from the workspace

_scratch_ws = None


def _seed_instructions(workspace, instructions, owned):
    """Write the workspace's ``AGENTS.md``. ``instructions=None`` clears it, but only in the
    scratch repo we own — a caller-supplied workspace is never stripped of its own file."""
    path = os.path.join(workspace, AGENTS_MD)
    if instructions is not None:
        with open(path, "w") as f:
            f.write(instructions if instructions.endswith("\n") else instructions + "\n")
    elif owned and os.path.exists(path):
        os.remove(path)          # the scratch repo is reused — don't leak a prior run's instructions
    return path


def ensure_git_workspace(path=None, prefix="wire-ws-", instructions=None) -> str:
    """Return a git workspace path. If ``path`` is given, use it; otherwise create (once) a
    reusable throwaway repo and return it on subsequent calls.

    ``instructions`` is markdown seeded as the workspace's ``AGENTS.md`` — the standing
    instructions both agy and codex load at startup, so the per-turn prompt can stay short::

        ws = ensure_git_workspace(instructions=open("prompts/blender_recon_single_instance.md").read())
        pycodex.ask("Reconstruct the GT model.", workspace=ws)

    Because the scratch repo is reused across runs, its ``AGENTS.md`` always reflects the
    latest call: passing ``None`` clears it rather than inheriting the previous run's task.
    """
    global _scratch_ws
    if path:
        _seed_instructions(path, instructions, owned=False)
        return path
    if _scratch_ws and os.path.isdir(os.path.join(_scratch_ws, ".git")):
        _seed_instructions(_scratch_ws, instructions, owned=True)
        return _scratch_ws
    d = tempfile.mkdtemp(prefix=prefix)
    repo = pygit2.init_repository(d)
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("# scratch workspace\n")
    _seed_instructions(d, instructions, owned=True)   # before the commit, so the repo starts clean
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("wirecap", "wirecap@local")
    repo.create_commit("HEAD", sig, sig, "init", tree, [])
    _scratch_ws = d
    return d
