"""Readers for codex's native session (rollout) store — the codex counterpart of
``pyagy.conversations``.

codex records every session as JSONL under ``~/.codex/sessions/YYYY/MM/DD/
rollout-<timestamp>-<session_id>.jsonl`` (override the root with ``CODEX_HOME``). Lines are
``{"timestamp", "type", "payload"}``; the types we read are:

  ``session_meta``   once, first line — carries ``payload.session_id`` and ``cwd``
  ``response_item``  the conversation items — ``payload.role`` / ``payload.content``

Read-only and stdlib-only: this is how a :class:`pycodex.Session` answers ``history()`` and how
``continue_latest()`` finds the newest session, mirroring what pyagy gets from agy's store.
"""
import json
import os

_ROLLOUT_PREFIX = "rollout-"


def sessions_root(home=None):
    """codex's session store root (``$CODEX_HOME/sessions``, default ``~/.codex/sessions``)."""
    base = home or os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(base, "sessions")


def list_rollouts(home=None):
    """Every rollout file, NEWEST FIRST (by mtime). Empty when the store is absent."""
    root = sessions_root(home)
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.startswith(_ROLLOUT_PREFIX) and fn.endswith(".jsonl"):
                p = os.path.join(dirpath, fn)
                try:
                    out.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    out.sort(reverse=True)
    return [p for _mt, p in out]


def _session_id_of(path):
    """The uuid tail of a rollout filename (``rollout-<ts>-<uuid>.jsonl``)."""
    stem = os.path.basename(path)[len(_ROLLOUT_PREFIX):].removesuffix(".jsonl")
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) >= 5 else stem


def find_rollout(session_id, home=None):
    """The rollout file for ``session_id``, or None. Matches the filename tail first (cheap),
    then falls back to reading each file's ``session_meta`` (covers a renamed file)."""
    if not session_id:
        return None
    paths = list_rollouts(home)
    for p in paths:
        if _session_id_of(p) == session_id:
            return p
    for p in paths:
        meta = read_meta(p)
        if meta and (meta.get("session_id") or meta.get("id")) == session_id:
            return p
    return None


def read_meta(path):
    """The ``session_meta`` payload of a rollout file (its first line), or None."""
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") == "session_meta":
                    return obj.get("payload") or {}
                return None      # session_meta is always first; don't scan the whole file
    except OSError:
        return None
    return None


def latest_session_id(home=None, cwd=None):
    """The newest session's id, optionally restricted to sessions started in ``cwd``.
    None when the store is empty — the caller then starts a fresh session."""
    for p in list_rollouts(home):
        if cwd:
            meta = read_meta(p) or {}
            if os.path.realpath(meta.get("cwd") or "") != os.path.realpath(cwd):
                continue
        return _session_id_of(p)
    return None


def read_transcript(session_id, home=None):
    """The stored transcript for ``session_id`` — a list of
    ``{step_index, role, type, content}`` in file order, mirroring the shape
    ``pyagy.conversations.read_transcript`` returns. Empty when the session is unknown."""
    path = find_rollout(session_id, home)
    if not path:
        return []
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "response_item":
                    continue
                p = obj.get("payload") or {}
                out.append({
                    "step_index": len(out),
                    "role": p.get("role"),
                    "type": p.get("type"),
                    "created_at": obj.get("timestamp"),
                    "content": _flatten(p.get("content")),
                })
    except OSError:
        return []
    return out


def _flatten(content):
    """codex content is a list of typed parts; join their text so a caller gets a plain string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict):
            t = c.get("text") or c.get("content")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)
