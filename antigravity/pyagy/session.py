"""Convenience wrappers over :class:`pyagy.agyprocess.AgyProcess` (the single agy launcher).

`run_print()` is the one-shot path (`agy --print <prompt>`) used by the TaskSolver backend: it
returns a dict whose ``result`` is agy's DECODED answer — collected from the turn the in-agy worker
streams home over the Connection (app-boundary text preferred, else the wire turn), falling back to
the filtered PTY transcript. `InteractiveSession` drives a multi-turn session (see
:class:`pyagy.Session` for the richer API). Both run instrumented on the pinned vendor/agy through
AgyProcess, which owns the PTY, git-workspace, argv, env, and conversation-scoping policy.
"""
from .conversations import ensure_git_workspace  # noqa: F401  (re-export — public API)
from ._term import answer_text, strip_ansi  # noqa: F401  (strip_ansi re-exported — public API)


def run_print(prompt, workdir=None, model=None, timeout=300, skip_permissions=False,
              extra_flags=None, conversation_id=None, continue_latest=False,
              data_dir=None, trust=True):
    """One-shot ``agy --print <prompt>`` → dict(result, transcript, exit_status, workspace).
    Instrumented via :class:`AgyProcess`; ``result`` is the decoded answer the in-agy worker streams
    home (app-boundary text preferred, else the wire turn), falling back to the filtered PTY
    transcript when nothing decoded. ``transcript`` is the raw PTY output (diagnostics).
    ``conversation_id`` resumes a stored conversation (``--conversation=<id>``); ``continue_latest``
    resumes the most recent; ``data_dir`` scopes the conversation store to a project repo; ``trust``
    pre-trusts the workspace."""
    from .agyprocess import AgyProcess
    p = AgyProcess(prompt=prompt, model=model, skip_permissions=skip_permissions,
                   extra_flags=extra_flags, workdir=workdir, conversation_id=conversation_id,
                   continue_latest=continue_latest, data_dir=data_dir, trust=trust)
    p.start()
    objs = p.collect(timeout=timeout)
    transcript = p.transcript
    p.close()
    app = [o["text"] for o in objs if o.get("kind") == "app_response" and o.get("text")]
    wire = [o["text"] for o in objs if o.get("kind") == "genai_turn" and o.get("text")]
    result = (max(app, key=len).strip() if app else
              max(wire, key=len).strip() if wire else answer_text(transcript))
    return {"result": result, "transcript": transcript,
            "exit_status": p.exit_status, "workspace": p.workspace}


class InteractiveSession:
    """Multi-turn agy session over :class:`AgyProcess`. Each :meth:`ask` submits a prompt (or the
    ``--prompt-interactive`` prefill on the first call) and returns that turn's decoded answer text.
    See :class:`pyagy.Session` for the richer API (decoded turns, resume, history)."""

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

    def ask(self, prompt=None, idle=6.0, timeout=180.0):
        """Submit ``prompt`` (or the prefill if None) and return the turn's answer text."""
        turns = self._agy.ask(prompt, idle=idle, timeout=timeout)
        return "\n".join(t.get("text", "") for t in turns if (t.get("text") or "").strip()).strip()

    def close(self):
        if self._agy is not None:
            self._agy.close(interrupt=True)
