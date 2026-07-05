"""Convenience wrappers over :class:`pyagy.agyprocess.AgyProcess` (the single agy launcher).

`run_print()` is the one-shot path (`agy --print <prompt>`) used by the TaskSolver backend:
it returns a dict with agy's answer text (shim log lines filtered out). `InteractiveSession`
drives a multi-turn TUI session. Both run **instrumented** (shim + capture on the pinned
vendor/agy) through AgyProcess, which owns the PTY, git-workspace, argv, env, and
conversation-scoping policy — these two are just its plain-CLI façade with the historic
return shapes.
"""
from .conversations import ensure_git_workspace  # noqa: F401  (re-export — public API)
from ._term import answer_text, strip_ansi  # noqa: F401  (strip_ansi re-exported — public API)


def run_print(prompt, workdir=None, model=None, timeout=300, skip_permissions=False,
              extra_flags=None, conversation_id=None, continue_latest=False,
              data_dir=None, trust=True):
    """One-shot ``agy --print <prompt>`` → dict(result, transcript, exit_status, workspace).
    Instrumented (shim + capture) via :class:`AgyProcess`; ``result`` is the answer text with
    our shim log lines filtered out. ``conversation_id`` resumes a stored conversation
    (``--conversation=<id>``, works in print mode) and ``continue_latest`` resumes the most
    recent (``--continue``); ``data_dir`` scopes the conversation store to a project repo;
    ``trust`` pre-trusts the workspace."""
    from .agyprocess import AgyProcess
    p = AgyProcess(prompt=prompt, model=model, skip_permissions=skip_permissions,
                   extra_flags=extra_flags, workdir=workdir, conversation_id=conversation_id,
                   continue_latest=continue_latest, data_dir=data_dir, trust=trust)
    p.start()
    transcript = p.read_until_exit(timeout=timeout)
    p.close()
    return {"result": answer_text(transcript), "transcript": transcript,
            "exit_status": p.exit_status, "workspace": p.workspace}


class InteractiveSession:
    """Multi-turn TUI session over :class:`AgyProcess` (plain-CLI ``--prompt-interactive``).
    See :class:`pyagy.Session` for the first-class multi-turn API with decoded turns, and
    test_scripts/agy_session.py for the capture-experiment harness."""

    def __init__(self, workdir=None, model=None):
        self.workdir = workdir
        self.model = model
        self._agy = None

    def start(self, prompt):
        from .agyprocess import AgyProcess
        self._agy = AgyProcess(persistent=True, prompt=prompt, model=self.model,
                               workdir=self.workdir)
        self._agy.start()
        return self

    def read_until_idle(self, idle=6.0, timeout=180.0):
        return self._agy.read_until_idle(idle=idle, timeout=timeout)

    def submit(self, text=""):
        self._agy.send_line(text)

    def close(self):
        if self._agy is not None:
            self._agy.close(interrupt=True)
