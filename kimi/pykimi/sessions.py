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
from typing import Final

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
    wire journal's conversation records. Older journals store complete messages in
    ``context.append_message``; Kimi 2.x also stores assistant steps and tool results
    in ``context.append_loop_event``. Each agent journal is folded independently.
    Empty when the session is unknown."""
    out: Final = []
    for path in _agent_wire_paths(session_id, home, agent):
        out.extend(_fold_transcript(_read_wire_path(path)))
    for index, entry in enumerate(out):
        entry["step_index"] = index
    return out


_INTERRUPTED_TOOL_OUTPUT: Final = (
    "Tool execution was interrupted before its result was recorded. "
    "Do not assume the tool completed successfully."
)


def _fold_transcript(records):
    """Project the v2 loopEventFold contract onto our plain-text transcript rows.

    Pending tools defer injected messages until their results arrive. Interrupted
    steps stay open for resumed content, while empty completed steps disappear.
    Like the upstream transcript reducer, EOF leaves an in-progress step visible.
    """
    out: Final = []
    pending: Final = set()
    deferred: Final = []
    open_entry = None
    open_uuid = None
    open_has_tools = False
    open_vacuous = True

    def row(role, content, time):
        return {"role": role, "type": "message", "created_at": time,
                "content": _flatten(content)}

    def flush_deferred():
        if not pending:
            out.extend(deferred)
            deferred.clear()

    def settle(time):
        nonlocal open_entry, open_uuid
        if open_entry is None:
            return
        for _ in pending:
            out.append(row("tool", _INTERRUPTED_TOOL_OUTPUT, time))
        pending.clear()
        flush_deferred()
        if not open_has_tools and open_vacuous:
            # Identity matters: two empty steps can have identical row values.
            out[:] = [entry for entry in out if entry is not open_entry]
        open_entry = None
        open_uuid = None

    for rec in records:
        if rec.get("type") == "context.append_message":
            message = rec.get("message") or {}
            entry = row(message.get("role"), message.get("content"), rec.get("time"))
            (deferred if pending else out).append(entry)
        elif rec.get("type") in ("context.clear", "context.apply_compaction"):
            # These records end the live fold; history remains a journal projection.
            if rec.get("keptUserMessageCount") is not None:
                settle(rec.get("time"))
            open_entry = None
            open_uuid = None
            pending.clear()
            deferred.clear()
        elif rec.get("type") == "context.append_loop_event":
            event = rec.get("event") or {}
            kind = event.get("type")
            if kind == "step.begin":
                settle(rec.get("time"))
                open_entry = row("assistant", "", rec.get("time"))
                out.append(open_entry)
                open_uuid = event.get("uuid")
                open_has_tools = False
                open_vacuous = True
            elif kind == "step.end":
                if event.get("finishReason") not in ("interrupted", "error"):
                    settle(rec.get("time"))
                    flush_deferred()
            elif kind in ("content.part", "tool.call"):
                if open_entry is None or event.get("stepUuid") != open_uuid:
                    continue
                if kind == "tool.call":
                    pending.add(event.get("toolCallId"))
                    open_has_tools = True
                else:
                    part = event.get("part") or {}
                    open_entry["content"] += _flatten([part])
                    if part.get("type") == "text":
                        vacuous = not (part.get("text") or "").strip()
                    elif part.get("type") == "think":
                        vacuous = ("encrypted" not in part
                                   and not (part.get("think") or "").strip())
                    else:
                        vacuous = False
                    open_vacuous = open_vacuous and vacuous
            elif kind == "tool.result" and event.get("toolCallId") in pending:
                pending.remove(event.get("toolCallId"))
                out.append(row("tool", (event.get("result") or {}).get("output"),
                               rec.get("time")))
                flush_deferred()
    return out


def _agent_wire_paths(session_id, home, agent):
    sdir: Final = find_session_dir(session_id, home)
    if not sdir:
        return
    agents_dir: Final = os.path.join(sdir, "agents")
    try:
        agent_ids: Final = [agent] if agent else sorted(os.listdir(agents_dir))
    except OSError:
        return
    for aid in agent_ids:
        yield os.path.join(agents_dir, aid, "wire.jsonl")


def _read_wire_path(path):
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def read_wire(session_id, home=None, agent=None):
    """The parsed ``wire.jsonl`` records for ``session_id`` — a list of dicts in file order,
    across every agent journal (``agents/<id>/wire.jsonl``), or just ``agent``'s when given.
    Empty when the session is unknown."""
    return [record for path in _agent_wire_paths(session_id, home, agent)
            for record in _read_wire_path(path)]
