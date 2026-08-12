"""Readers for kimi-code's native session store — the kimi counterpart of ``pycodex.sessions``.

kimi-code (agent-core-v2) keeps everything under ``$KIMI_CODE_HOME`` (default ``~/.kimi-code``):

  ``session_index.jsonl``      append-only index; live lines are
                               ``{"sessionId", "sessionDir", "workDir"}`` and a deletion appends
                               a ``{"sessionId", "deleted": true}`` tombstone
                               (agent-core-v2 sessionLifecycleService).
  ``sessions/<wd_key>/<session_id>/``
                               the session dir (``wd_key`` = ``wd_<slug>_<sha256[:12]>`` of the
                               working directory); holds ``state.json`` and per-agent
                               ``agents/<agent_id>/wire.jsonl`` journals.

Read-only and stdlib-only: this is how :func:`pykimi.ask` reports the store-read
``KimiResponse.session_id`` and how a caller resolves a session's wire journals.
"""
import json
import os

INDEX_NAME = "session_index.jsonl"


def home_root(home=None):
    """kimi-code's store root (``$KIMI_CODE_HOME``, default ``~/.kimi-code``)."""
    return (home or os.environ.get("KIMI_CODE_HOME")
            or os.path.join(os.path.expanduser("~"), ".kimi-code"))


def load_index(home=None):
    """The folded session index: ``{session_id: {"sessionId", "sessionDir", "workDir"}}`` in
    append order, tombstones applied. Empty when the store is absent."""
    path = os.path.join(home_root(home), INDEX_NAME)
    entries = {}
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                sid = obj.get("sessionId")
                if not sid:
                    continue
                if obj.get("deleted"):
                    entries.pop(sid, None)
                else:
                    entries[sid] = obj
    except OSError:
        return {}
    return entries


def latest_session_id(home=None, cwd=None):
    """The most recently indexed live session's id, optionally restricted to sessions whose
    ``workDir`` is ``cwd``. None when the store is empty — the caller then treats the run as a
    fresh session."""
    want = os.path.realpath(cwd) if cwd else None
    latest = None
    for sid, entry in load_index(home).items():   # dict preserves append order
        if want and os.path.realpath(entry.get("workDir") or "") != want:
            continue
        latest = sid
    return latest


def find_session_dir(session_id, home=None):
    """The on-disk session dir for ``session_id``, or None. The index's recorded ``sessionDir``
    wins when it exists (it is absolute); a relocated store (the harness archives task homes)
    falls back to scanning ``<home>/sessions/*/<session_id>``."""
    if not session_id:
        return None
    entry = load_index(home).get(session_id)
    if entry:
        recorded = entry.get("sessionDir")
        if recorded and os.path.isdir(recorded):
            return recorded
    root = os.path.join(home_root(home), "sessions")
    try:
        wd_keys = sorted(os.listdir(root))
    except OSError:
        return None
    for wd in wd_keys:
        candidate = os.path.join(root, wd, session_id)
        if os.path.isdir(candidate):
            return candidate
    return None


def _flatten(content):
    """kimi message content is a list of typed parts; join their text so a caller
    gets a plain string (the same contract as pycodex.sessions._flatten)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict):
            text = c.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


def read_transcript(session_id, home=None, agent=None):
    """The stored transcript for ``session_id`` — a list of
    ``{step_index, role, type, created_at, content}`` in journal order, the same
    shape :func:`pycodex.sessions.read_transcript` returns, projected from the
    wire journal's conversation records (``context.append_message`` carries every
    message appended to the model context: user turns, assistant replies, tool
    results). Empty when the session is unknown."""
    out = []
    for rec in read_wire(session_id, home=home, agent=agent):
        if rec.get("type") != "context.append_message":
            continue
        message = rec.get("message") or {}
        out.append({
            "step_index": len(out),
            "role": message.get("role"),
            "type": "message",
            "created_at": rec.get("time"),
            "content": _flatten(message.get("content")),
        })
    return out


def read_wire(session_id, home=None, agent=None):
    """The parsed ``wire.jsonl`` records for ``session_id`` — a list of dicts in file order,
    across every agent journal (``agents/<id>/wire.jsonl``), or just ``agent``'s when given.
    Empty when the session is unknown."""
    sdir = find_session_dir(session_id, home)
    if not sdir:
        return []
    agents_dir = os.path.join(sdir, "agents")
    try:
        agent_ids = [agent] if agent else sorted(os.listdir(agents_dir))
    except OSError:
        return []
    out = []
    for aid in agent_ids:
        path = os.path.join(agents_dir, aid, "wire.jsonl")
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return out
