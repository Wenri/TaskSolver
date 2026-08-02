"""A throwaway git workspace — shared by every wrapper.

Both agy and codex refuse to run outside a git repo (agy always; codex unless
``--skip-git-repo-check``), and both wrappers want a disposable one when the caller
doesn't supply a workspace. One reusable scratch repo is created lazily and reused.

The workspace also carries what the run knows, in the two forms both CLIs discover natively:

* ``AGENTS.md`` — standing instructions applied for the whole session. agy walks up from the cwd
  to the repo root loading ``GEMINI.md``/``AGENTS.md``; codex reads ``AGENTS.md`` plus an
  ``AGENTS.override.md``.
* ``.agents/skills/<name>/SKILL.md`` — skills, loaded lazily: only each skill's name and
  description sit in context until the model decides to use one. Both CLIs discover this path by
  walking up from the cwd to the repo root, so seeding it here serves both wrappers.

Seeding these in one place means the per-turn prompt stays short — see
``.agents/skills/blender-gt-reconstruction`` for a skill written to be delivered this way.
"""
import os
import shutil
import tempfile
import warnings

import pygit2

AGENTS_MD = "AGENTS.md"                            # instruction file BOTH agy and codex read
SKILLS_SUBDIR = os.path.join(".agents", "skills")  # skill root BOTH agy and codex discover

_scratch_ws = None


def _in_git_repo(path):
    """True if ``path`` or an ancestor holds a ``.git`` — skill discovery needs a repo root."""
    p = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


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


def _seed_skills(workspace, skills, owned):
    """Copy skill directories into ``<workspace>/.agents/skills/``.

    Copied, never symlinked: the run then owns a frozen snapshot of the skill text (so a capture
    correlates with exactly what was in context), and nothing resolves outside the workspace.
    In the scratch repo we own the root is reset to exactly ``skills``, so ``None`` clears it; a
    caller-supplied workspace only ever has the named skills copied in.
    """
    root = os.path.join(workspace, SKILLS_SUBDIR)
    if owned and os.path.isdir(root):
        shutil.rmtree(root)      # reused scratch repo — don't leak a prior run's skills
    for src in skills or ():
        src = os.path.abspath(src)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            raise ValueError(f"not a skill directory (no SKILL.md): {src}")
        dest = os.path.join(root, os.path.basename(src.rstrip(os.sep)))
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
    return root


def ensure_git_workspace(path=None, prefix="wire-ws-", instructions=None, skills=None) -> str:
    """Return a git workspace path. If ``path`` is given, use it; otherwise create (once) a
    reusable throwaway repo and return it on subsequent calls.

    ``instructions`` is markdown seeded as the workspace's ``AGENTS.md`` — standing instructions
    both CLIs apply for the whole session. ``skills`` is an iterable of skill directories (each
    must contain a ``SKILL.md``) copied into ``<ws>/.agents/skills/``, which both CLIs discover
    and load lazily::

        ws = ensure_git_workspace(
            instructions="Task: reconstruct the GT per the blender-gt-reconstruction skill.",
            skills=[".agents/skills/blender-gt-reconstruction"])
        pycodex.ask("Reconstruct the GT model.", workspace=ws)

    Because the scratch repo is reused across runs, both seeds always reflect the latest call:
    passing ``None`` clears them rather than inheriting the previous run's task. **Pass the
    returned workspace to ``ask()``** — a bare ``ask(prompt)`` resolves the scratch repo with no
    seeds and therefore clears them. A caller-supplied workspace is only ever written to, never
    stripped of its own ``AGENTS.md`` or skills.
    """
    global _scratch_ws
    if path:
        if skills and not _in_git_repo(path):
            warnings.warn(f"{path} is not inside a git repo — agy and codex only discover "
                          f"{SKILLS_SUBDIR} by walking up to a repo root, so the seeded skills "
                          f"will be invisible there", stacklevel=2)
        _seed_instructions(path, instructions, owned=False)
        _seed_skills(path, skills, owned=False)
        return path
    if _scratch_ws and os.path.isdir(os.path.join(_scratch_ws, ".git")):
        _seed_instructions(_scratch_ws, instructions, owned=True)
        _seed_skills(_scratch_ws, skills, owned=True)
        return _scratch_ws
    d = tempfile.mkdtemp(prefix=prefix)
    repo = pygit2.init_repository(d)
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("# scratch workspace\n")
    _seed_instructions(d, instructions, owned=True)   # before the commit, so the repo starts clean
    _seed_skills(d, skills, owned=True)
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("wirecap", "wirecap@local")
    repo.create_commit("HEAD", sig, sig, "init", tree, [])
    _scratch_ws = d
    return d
