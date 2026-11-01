"""Readers for codex's native session (rollout) store — the codex counterpart of
``pyagy.conversations``.

codex records every session as JSONL under ``~/.codex/sessions/YYYY/MM/DD/
rollout-<timestamp>-<thread_id>[_<rollout_id>].jsonl`` (override the root with ``CODEX_HOME``).
Reverting a thread creates a new rollout ID while keeping the thread ID used for resume. Lines are
``{"timestamp", "type", "payload"}``; the types we read are:

  ``session_meta``   once, first line — carries the thread's ``payload.id`` and ``cwd``
  ``response_item``  the conversation items — ``payload.role`` / ``payload.content``

Read-only and stdlib-only: this is how a :class:`pycodex.Session` answers ``history()`` and how
``continue_latest()`` finds the newest session, mirroring what pyagy gets from agy's store.
"""
import os
from typing import Final, TypedDict

from wirecap.types import JSONObject, JSONValue, json_loads_object, json_object

type PathInput = str | os.PathLike[str]


class TranscriptRow(TypedDict):
    step_index: int
    role: JSONValue
    type: JSONValue
    created_at: JSONValue
    content: str
from uuid import UUID

_ROLLOUT_PREFIX = "rollout-"


def sessions_root(home: PathInput | None = None) -> str:
    """codex's session store root (``$CODEX_HOME/sessions``, default ``~/.codex/sessions``)."""
    base = home or os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(base, "sessions")


def list_rollouts(home: PathInput | None = None) -> list[str]:
    """Every rollout file, NEWEST FIRST (by mtime). Empty when the store is absent."""
    root = sessions_root(home)
    out: list[tuple[float, str]] = []
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


def _session_id_of(path: PathInput) -> str | None:
    """The stable thread UUID, before any replacement-rollout suffix, or None."""
    stem: Final = os.path.basename(path)[len(_ROLLOUT_PREFIX):].removesuffix(".jsonl")
    parts: Final = stem.partition("_")[0].split("-")
    try:
        return str(UUID("-".join(parts[-5:])))
    except ValueError:
        return None


def _meta_session_id(meta: JSONObject) -> str | None:
    """Resume uses the thread ID; session_id may instead identify its root ancestor."""
    value = meta.get("id") or meta.get("session_id")
    return value if isinstance(value, str) else None


def find_rollout(session_id: str | None, home: PathInput | None = None) -> str | None:
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
        if meta and _meta_session_id(meta) == session_id:
            return p
    return None


def read_meta(path: PathInput) -> JSONObject | None:
    """The ``session_meta`` payload of a rollout file (its first line), or None."""
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    obj = json_loads_object(line)
                except ValueError:
                    continue
                if obj.get("type") == "session_meta":
                    return json_object(obj.get("payload") or {})
                return None      # session_meta is always first; don't scan the whole file
    except OSError:
        return None
    return None


def latest_session_id(home: PathInput | None = None, cwd: PathInput | None = None) -> str | None:
    """The newest session's id, optionally restricted to sessions started in ``cwd``.
    None when the store is empty — the caller then starts a fresh session."""
    for p in list_rollouts(home):
        meta = None
        if cwd:
            meta = read_meta(p) or {}
            recorded_cwd = meta.get("cwd")
            if not isinstance(recorded_cwd, str) or os.path.realpath(recorded_cwd) != os.path.realpath(cwd):
                continue
        session_id = _session_id_of(p)
        if session_id:
            return session_id
        session_id = _meta_session_id(meta if meta is not None else read_meta(p) or {})
        if session_id:
            return session_id
    return None


def read_transcript(session_id: str | None, home: PathInput | None = None) -> list[TranscriptRow]:
    """The stored transcript for ``session_id`` — a list of
    ``{step_index, role, type, content}`` in file order, mirroring the shape
    ``pyagy.conversations.read_transcript`` returns. Empty when the session is unknown."""
    path = find_rollout(session_id, home)
    if not path:
        return []
    out: list[TranscriptRow] = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    obj = json_loads_object(line)
                except ValueError:
                    continue
                if obj.get("type") != "response_item":
                    continue
                p = json_object(obj.get("payload") or {})
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


def _flatten(content: JSONValue) -> str:
    """codex content is a list of typed parts; join their text so a caller gets a plain string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for c in content:
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict):
            t = c.get("text") or c.get("content")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)
