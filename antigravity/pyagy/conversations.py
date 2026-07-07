"""Read-only view of agy's native conversation store — the durable side of a
session-capable ``pyagy`` — plus the two write-side helpers a session needs
(pre-trusting a workspace and scoping the data dir to a project repo).

agy persists every conversation under ``~/.gemini/antigravity-cli/`` and can resume one
on a fresh launch (``agy --conversation=<id>`` / ``agy --continue``). This module reads
that store so pyagy can *list* past conversations, read their *history*, and *resolve the
ID* of a conversation a run just created/continued — without depending on agy internals
beyond the on-disk layout:

    conversations/<uuid>.db                     one SQLite DB per conversation; `steps` = turns
    brain/<uuid>/.system_generated/logs/transcript.jsonl   human-readable turn log
    conversation_summaries.db                   intended index — EMPTY here (failed migration),
                                                so we enumerate conversations/*.db by mtime
    history.jsonl                               interactive REPL input: {conversationId, workspace, ...}
    settings.json                               {model, enableTelemetry, trustedWorkspaces[]}

The store root is ``<GeminiDir>/<app_data_dir>`` where GeminiDir is ``$HOME/.gemini``
(agy ignores GEMINI_DIR/GEMINI_HOME at runtime — verified — so the ``home`` argument here
mirrors an ``HOME`` override, which IS how a run gets scoped) and ``app_data_dir`` defaults
to ``antigravity-cli``. Everything is best-effort and read-only where it reads (SQLite opened
``?mode=ro``): a missing/locked file yields ``None``/``[]`` rather than raising, because this
rides alongside a live agy that may be writing the same store.
"""
import glob
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass

# The throwaway-git-workspace helper is provider-neutral (agy + codex both need one) and lives in
# the shared runtime package; re-exported here so pyagy.ensure_git_workspace and _pty's import are
# unchanged.
from wirecap.runtime.workspace import ensure_git_workspace  # noqa: F401


def _gemini_dir(home=None):
    """agy's base config dir: ``<home>/.gemini`` when a scoped ``home`` is given (an HOME
    override), else ``$HOME/.gemini`` — agy's hardcoded default. (GEMINI_DIR/GEMINI_HOME env
    are NOT honored by the binary at runtime, so scoping is done via HOME, not those.)"""
    if home:
        return os.path.join(os.path.abspath(home), ".gemini")
    return os.path.expanduser("~/.gemini")


def store_root(app_data_dir=None, home=None):
    """The antigravity-cli data root. ``app_data_dir`` mirrors agy's ``--app_data_dir``
    (relative to the gemini dir, default ``antigravity-cli``); ``home`` scopes the whole
    gemini tree (as an HOME override would)."""
    adir = app_data_dir or "antigravity-cli"
    if os.path.isabs(adir):
        return adir
    return os.path.join(_gemini_dir(home), adir)


def conversations_dir(app_data_dir=None, home=None):
    return os.path.join(store_root(app_data_dir, home), "conversations")


def db_path(conversation_id, app_data_dir=None, home=None):
    return os.path.join(conversations_dir(app_data_dir, home), f"{conversation_id}.db")


def transcript_path(conversation_id, app_data_dir=None, home=None):
    return os.path.join(store_root(app_data_dir, home), "brain", conversation_id,
                        ".system_generated", "logs", "transcript.jsonl")


@dataclass
class ConversationInfo:
    id: str
    last_modified: float = 0.0          # epoch seconds (db mtime, or summaries last_modified)
    step_count: int = None              # rows in the `steps` table (turns), best-effort
    title: str = ""                     # first user prompt (cleaned), truncated
    preview: str = ""                   # first user prompt (cleaned), longer
    workspace: str = None               # from history.jsonl, best-effort
    status: str = None
    db: str = ""

    def __str__(self):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.last_modified)) \
            if self.last_modified else "?"
        n = "?" if self.step_count is None else self.step_count
        return f"{self.id}  [{when}, {n} steps]  {self.title}"


# --- low-level readers (all best-effort, read-only) --------------------------
def _ro_connect(path):
    """Open a SQLite DB read-only (respects any WAL of a live agy). None on failure."""
    if not os.path.exists(path):
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None


def _step_count(path):
    con = _ro_connect(path)
    if con is None:
        return None
    try:
        return con.execute("select count(*) from steps").fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        con.close()


_TAGS = ("<USER_REQUEST>", "</USER_REQUEST>")


def _clean_user_text(content):
    """Pull the human prompt out of a transcript USER_INPUT ``content`` blob (agy wraps it
    in ``<USER_REQUEST>`` plus ``<ADDITIONAL_METADATA>``/``<USER_SETTINGS_CHANGE>`` sections)."""
    if not content:
        return ""
    text = content
    if _TAGS[0] in text:
        seg = text.split(_TAGS[0], 1)[1]
        text = seg.split(_TAGS[1], 1)[0] if _TAGS[1] in seg else seg
    else:
        cut = text.find("\n<")            # drop any trailing <SECTION>...</SECTION> metadata
        if cut != -1:
            text = text[:cut]
    return text.strip()


def read_transcript(conversation_id, app_data_dir=None, home=None):
    """Parsed human-readable turns for a conversation, or ``[]``. Each item:
    ``{step_index, role, type, status, created_at, content}`` where ``role`` is
    ``"user"`` for USER_INPUT else ``"agent"`` and ``content`` is cleaned of agy's tags."""
    path = transcript_path(conversation_id, app_data_dir, home)
    if not os.path.exists(path):
        return []
    turns = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                is_user = obj.get("type") == "USER_INPUT"
                content = obj.get("content", "")
                turns.append({
                    "step_index": obj.get("step_index"),
                    "role": "user" if is_user else "agent",
                    "type": obj.get("type"),
                    "status": obj.get("status"),
                    "created_at": obj.get("created_at"),
                    "content": _clean_user_text(content) if is_user else content,
                })
    except OSError:
        return turns
    return turns


def _first_user_text(conversation_id, app_data_dir=None, home=None):
    for t in read_transcript(conversation_id, app_data_dir, home):
        if t["role"] == "user" and t["content"]:
            return t["content"]
    return ""


def _history_index(app_data_dir=None, home=None):
    """``{conversation_id: {"workspace", "display", "timestamp"}}`` from history.jsonl
    (interactive input log; last entry per id wins). Best-effort → ``{}``."""
    path = os.path.join(store_root(app_data_dir, home), "history.jsonl")
    idx = {}
    if not os.path.exists(path):
        return idx
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cid = obj.get("conversationId")
                if cid:
                    idx[cid] = {"workspace": obj.get("workspace"),
                                "display": obj.get("display"),
                                "timestamp": obj.get("timestamp")}
    except OSError:
        pass
    return idx


# --- summaries index (used only when populated) ------------------------------
def _from_summaries(limit, app_data_dir, home):
    """Rows from ``conversation_summaries.db`` if it has any (it's empty on installs with a
    failed migration — then this returns ``None`` and the caller falls back to mtime scan)."""
    path = os.path.join(store_root(app_data_dir, home), "conversation_summaries.db")
    con = _ro_connect(path)
    if con is None:
        return None
    try:
        rows = con.execute(
            "select conversation_id, title, preview, step_count, last_modified_time, "
            "workspace_uris, status from conversation_summaries "
            "order by last_modified_time desc").fetchall()
    except sqlite3.Error:
        con.close()
        return None
    con.close()
    if not rows:
        return None
    out = []
    for cid, title, preview, step_count, lmt, wuris, status in rows:
        out.append(ConversationInfo(
            id=cid, title=(title or preview or "")[:80], preview=preview or title or "",
            step_count=step_count, last_modified=_epoch(lmt), workspace=wuris,
            status=status, db=db_path(cid, app_data_dir, home)))
    return out[:limit] if limit else out


def _epoch(datetime_str):
    """Best-effort ISO/sqlite-datetime → epoch seconds; 0.0 on failure."""
    if not datetime_str:
        return 0.0
    if isinstance(datetime_str, (int, float)):
        return float(datetime_str)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return time.mktime(time.strptime(str(datetime_str).split("+")[0].strip(), fmt))
        except ValueError:
            continue
    return 0.0


# --- public listing / resolution ---------------------------------------------
def _all_db_paths(app_data_dir=None, home=None):
    return glob.glob(os.path.join(conversations_dir(app_data_dir, home), "*.db"))


def _safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def list_conversations(limit=None, app_data_dir=None, enrich=True, home=None):
    """Past conversations, newest first. Prefers ``conversation_summaries.db`` when populated;
    otherwise enumerates ``conversations/*.db`` by mtime (the reliable path — the summaries
    table is empty on this install). ``enrich`` reads each returned conversation's step count
    and first-user-prompt title (only for the ``limit`` returned, so a small limit stays cheap).
    ``home`` scopes to a repo-scoped data dir (see :func:`prepare_scoped_home`)."""
    summary = _from_summaries(limit, app_data_dir, home)
    if summary is not None:
        return summary
    paths = _all_db_paths(app_data_dir, home)
    paths.sort(key=_safe_mtime, reverse=True)
    if limit:
        paths = paths[:limit]
    hist = _history_index(app_data_dir, home) if enrich else {}
    out = []
    for p in paths:
        cid = os.path.splitext(os.path.basename(p))[0]
        info = ConversationInfo(id=cid, last_modified=_safe_mtime(p), db=p)
        if enrich:
            info.step_count = _step_count(p)
            text = _first_user_text(cid, app_data_dir, home)
            info.title = text[:80]
            info.preview = text[:200]
            info.workspace = (hist.get(cid) or {}).get("workspace")
        out.append(info)
    return out


def latest_conversation_id(app_data_dir=None, home=None):
    """The conversation ID with the newest ``conversations/*.db`` mtime, or ``None``."""
    paths = _all_db_paths(app_data_dir, home)
    if not paths:
        return None
    return os.path.splitext(os.path.basename(max(paths, key=_safe_mtime)))[0]


# --- resolving the ID a run just created / continued -------------------------
def snapshot(app_data_dir=None, home=None):
    """Record ``{conversation_id: mtime}`` for every stored conversation *now*. Pass to
    :func:`capture_conversation_id` after a run to find which conversation it created (new
    id) or continued (mtime advanced)."""
    return {"mtimes": {os.path.splitext(os.path.basename(p))[0]: _safe_mtime(p)
                       for p in _all_db_paths(app_data_dir, home)}}


def _id_from_capture(capture_path):
    """First ``{"kind":"conversation_id","id":...}`` event in the capture JSONL, or ``None``.
    Emitted by the FILE_OPEN hook (AGY_PROC_CONV_ID) from the conversation-store path — exact
    and in-process. FIRST = the main/top-level conversation (its store opens before any
    subagent's)."""
    if not capture_path or not os.path.exists(capture_path):
        return None
    try:
        with open(capture_path, errors="replace") as f:
            for line in f:
                if '"conversation_id"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("kind") == "conversation_id" and obj.get("id"):
                    return obj["id"]
    except OSError:
        return None
    return None


def capture_conversation_id(snapshot=None, capture_path=None, app_data_dir=None, home=None):
    """Resolve the conversation a just-finished run created or continued, most-reliable first:
      1. a ``conversation_id`` event from the FILE_OPEN hook in the capture JSONL (exact,
         in-process — needs AGY_PROC_CONV_ID + an instrumented run);
      2. vs the pre-run ``snapshot``: the newest conversation that is NEW or whose mtime
         advanced (covers both create and resume);
      3. else the newest conversation overall.
    Returns the conversation ID, or ``None`` if the store is empty."""
    cid = _id_from_capture(capture_path)
    if cid:
        return cid
    cur = {os.path.splitext(os.path.basename(p))[0]: _safe_mtime(p)
           for p in _all_db_paths(app_data_dir, home)}
    if snapshot:
        prev = snapshot.get("mtimes", {})
        changed = [c for c, m in cur.items() if c not in prev or m > prev[c] + 1e-6]
        if changed:
            return max(changed, key=lambda c: cur[c])
    if cur:
        return max(cur, key=lambda c: cur[c])
    return None


# --- write side: workspace trust + repo-scoped data dir ----------------------
def trust_workspace(workspace, home=None, app_data_dir=None):
    """Add ``workspace`` (absolute) to ``trustedWorkspaces`` in the (scoped or global)
    ``settings.json`` — the same thing accepting agy's interactive "trust this folder" menu
    persists, so a live/interactive session won't block on it. Idempotent + atomic;
    best-effort (returns False on any error rather than raising)."""
    if not workspace:
        return False
    ws = os.path.abspath(workspace)
    path = os.path.join(store_root(app_data_dir, home), "settings.json")
    try:
        data = {}
        if os.path.exists(path):
            with open(path, errors="replace") as f:
                data = json.load(f) or {}
        tw = data.get("trustedWorkspaces") or []
        if ws in tw:
            return True
        tw.append(ws)
        data["trustedWorkspaces"] = tw
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".pyagy.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)                 # atomic, so a concurrent agy can't see a half file
        return True
    except (OSError, ValueError):
        return False


def prepare_scoped_home(data_dir, app_data_dir=None):
    """Scope agy's data dir to ``data_dir`` (a project repo): a run launched with
    ``HOME=data_dir`` keeps its whole conversation store under ``data_dir/.gemini/`` instead
    of the global ``~/.gemini``. agy reads GeminiDir as ``$HOME/.gemini`` and looks for its
    login token there, so we **seed** the scoped tree with symlinks to the real
    ``antigravity-oauth-token`` + ``installation_id`` (login stays valid, and token refresh in
    the real store still applies) and the shared ``config/`` dir, and drop a project-local
    ``settings.json`` (carrying the model, empty trust list). Idempotent. Returns the scoped
    store root (``data_dir/.gemini/<app_data_dir>``). See [[agy-native-sessions]]."""
    real_gem = os.path.expanduser("~/.gemini")
    real_app = os.path.join(real_gem, "antigravity-cli")
    gem = _gemini_dir(data_dir)                          # <data_dir>/.gemini
    root = store_root(app_data_dir, home=data_dir)       # <data_dir>/.gemini/<app_data_dir>
    os.makedirs(root, exist_ok=True)

    def _link(src, dst):
        if os.path.exists(src) and not os.path.lexists(dst):
            try:
                os.symlink(src, dst)
            except OSError:
                pass

    for name in ("antigravity-oauth-token", "installation_id"):
        _link(os.path.join(real_app, name), os.path.join(root, name))
    _link(os.path.join(real_gem, "config"), os.path.join(gem, "config"))

    # project-local settings.json (carry model/telemetry; trust starts empty and is filled by
    # trust_workspace) — only if absent, so we never clobber a scoped store agy already owns.
    settings = os.path.join(root, "settings.json")
    if not os.path.exists(settings):
        seed = {}
        try:
            with open(os.path.join(real_app, "settings.json"), errors="replace") as f:
                g = json.load(f) or {}
            for k in ("model", "enableTelemetry"):
                if k in g:
                    seed[k] = g[k]
        except (OSError, ValueError):
            pass
        seed["trustedWorkspaces"] = []
        try:
            with open(settings, "w") as f:
                json.dump(seed, f, indent=2)
        except OSError:
            pass
    return root


def scope_for_run(workspace, data_dir=None, trust=True, app_data_dir=None):
    """Prep native-store scoping + workspace trust for one run. If ``data_dir`` is set, seed a
    repo-scoped agy home (login preserved). Pre-trust ``workspace`` in the (scoped or global)
    ``settings.json`` unless ``trust=False``. Returns ``(home, env_overrides)`` — ``home`` is
    ``data_dir`` (or None), ``env_overrides`` sets ``HOME`` for the child so agy uses the
    scoped tree. Thread ``home`` back into the store readers/capture."""
    home, env_overrides = None, {}
    if data_dir:
        prepare_scoped_home(data_dir, app_data_dir)
        home = data_dir
        env_overrides["HOME"] = os.path.abspath(data_dir)
    if trust:
        trust_workspace(workspace, home=home, app_data_dir=app_data_dir)
    return home, env_overrides
