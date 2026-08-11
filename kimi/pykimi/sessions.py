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
