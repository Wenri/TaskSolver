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
mirrors an ``HOME`` override, which IS how a repo-scoped run gets isolated). By default
pyagy uses ``app_data_dir="pyagy"`` (``~/.gemini/pyagy`` via ``agy --app_data_dir=pyagy``),
giving automated ``pyagy`` / ``TaskSolver`` calls their own conversation/history store so
they never evict interactive ``agy`` sessions in ``~/.gemini/antigravity-cli``, while
sharing global ``~/.gemini/config``, keyring/file login, and global ``settings.json``
preferences. Everything is best-effort and read-only where it reads (SQLite opened
``?mode=ro``): a missing/locked file yields ``None``/``[]`` rather than raising, because this
rides alongside a live agy that may be writing the same store.
"""
from collections.abc import Generator
from contextlib import contextmanager
import fcntl
import glob
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from typing import Final, TypedDict, cast

from wirecap.types import JSONObject, JSONValue, json_array, json_load, json_loads_object, json_object

type PathInput = str | os.PathLike[str]

#: Interactive ``agy`` CLI's global store name under ``~/.gemini/``.
GLOBAL_APP_DATA_DIR: Final = "antigravity-cli"
#: Dedicated store name under ``~/.gemini/`` for programmatic ``pyagy`` / ``TaskSolver`` runs.
DEFAULT_APP_DATA_DIR: Final = "pyagy"
#: Environment override for ``pyagy``'s default ``app_data_dir`` (relative to ``~/.gemini``).
APP_DATA_DIR_ENV: Final = "PYAGY_APP_DATA_DIR"


class ConversationSnapshot(TypedDict):
    mtimes: dict[str, float]


class TranscriptTurn(TypedDict):
    step_index: JSONValue
    role: str
    type: JSONValue
    status: JSONValue
    created_at: JSONValue
    content: str


class HistoryEntry(TypedDict):
    workspace: str | None
    display: JSONValue
    timestamp: JSONValue

# The throwaway-git-workspace helper is provider-neutral (agy + codex both need one) and lives in
# the shared runtime package; re-exported here so pyagy.ensure_git_workspace and _pty's import are
# unchanged.
from wirecap.runtime.workspace import ensure_git_workspace as ensure_git_workspace


def _gemini_dir(home: PathInput | None = None) -> str:
    """agy's base config dir: ``<home>/.gemini`` when a scoped ``home`` is given (an HOME
    override), else ``$HOME/.gemini`` — agy's hardcoded default. (GEMINI_DIR/GEMINI_HOME env
    are NOT honored by the binary at runtime, so scoping is done via HOME, not those.)"""
    if home:
        return os.path.join(os.path.abspath(home), ".gemini")
    return os.path.expanduser("~/.gemini")


def resolve_app_data_dir(app_data_dir: str | None = None, home: PathInput | None = None) -> str:
    """Resolve the effective ``app_data_dir`` name.

    Precedence: explicit ``app_data_dir`` > ``$PYAGY_APP_DATA_DIR`` > ``"antigravity-cli"``
    when ``home`` already scopes the ``.gemini`` tree (``data_dir=``) > ``"pyagy"`` when
    using the global ``~/.gemini`` root so programmatic runs have their own history store.
    """
    if app_data_dir:
        return app_data_dir
    env_dir = os.environ.get(APP_DATA_DIR_ENV)
    if env_dir:
        return env_dir
    if home:
        return GLOBAL_APP_DATA_DIR
    return DEFAULT_APP_DATA_DIR


def store_root(app_data_dir: str | None = None, home: PathInput | None = None) -> str:
    """The antigravity data root. ``app_data_dir`` mirrors agy's ``--app_data_dir``
    (relative to the gemini dir, default ``pyagy`` globally or ``antigravity-cli`` inside a
    scoped ``home``); ``home`` scopes the whole gemini tree (as an HOME override would)."""
    adir = resolve_app_data_dir(app_data_dir, home)
    if os.path.isabs(adir):
        return adir
    return os.path.join(_gemini_dir(home), adir)


def conversations_dir(app_data_dir: str | None = None, home: PathInput | None = None) -> str:
    return os.path.join(store_root(app_data_dir, home), "conversations")


def db_path(conversation_id: str, app_data_dir: str | None = None, home: PathInput | None = None) -> str:
    return os.path.join(conversations_dir(app_data_dir, home), f"{conversation_id}.db")


def transcript_path(conversation_id: str, app_data_dir: str | None = None, home: PathInput | None = None) -> str:
    return os.path.join(store_root(app_data_dir, home), "brain", conversation_id,
                        ".system_generated", "logs", "transcript.jsonl")


@dataclass
class ConversationInfo:
    id: str
    last_modified: float = 0.0          # epoch seconds (db mtime, or summaries last_modified)
    step_count: int | None = None              # rows in the `steps` table (turns), best-effort
    title: str = ""                     # first user prompt (cleaned), truncated
    preview: str = ""                   # first user prompt (cleaned), longer
    workspace: str | None = None               # from history.jsonl, best-effort
    status: str | None = None
    db: str = ""

    def __str__(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.last_modified)) \
            if self.last_modified else "?"
        n = "?" if self.step_count is None else self.step_count
        return f"{self.id}  [{when}, {n} steps]  {self.title}"


# --- low-level readers (all best-effort, read-only) --------------------------
def _ro_connect(path: PathInput) -> sqlite3.Connection | None:
    """Open a SQLite DB read-only (respects any WAL of a live agy). None on failure."""
    if not os.path.exists(path):
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None


def _step_count(path: PathInput) -> int | None:
    con = _ro_connect(path)
    if con is None:
        return None
    try:
        return cast(tuple[int], con.execute("select count(*) from steps").fetchone())[0]
    except sqlite3.Error:
        return None
    finally:
        con.close()


_TAGS = ("<USER_REQUEST>", "</USER_REQUEST>")


def _clean_user_text(content: str) -> str:
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


def read_transcript(conversation_id: str, app_data_dir: str | None = None, home: PathInput | None = None) -> list[TranscriptTurn]:
    """Parsed human-readable turns for a conversation, or ``[]``. Each item:
    ``{step_index, role, type, status, created_at, content}`` where ``role`` is
    ``"user"`` for USER_INPUT else ``"agent"`` and ``content`` is cleaned of agy's tags."""
    path = transcript_path(conversation_id, app_data_dir, home)
    if not os.path.exists(path):
        return []
    turns: list[TranscriptTurn] = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json_loads_object(line)
                except ValueError:
                    continue
                is_user = obj.get("type") == "USER_INPUT"
                content = obj.get("content", "")
                if not isinstance(content, str):
                    continue
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


def _first_user_text(conversation_id: str, app_data_dir: str | None = None, home: PathInput | None = None) -> str:
    for t in read_transcript(conversation_id, app_data_dir, home):
        if t["role"] == "user" and t["content"]:
            return t["content"]
    return ""


def _history_index(app_data_dir: str | None = None, home: PathInput | None = None) -> dict[str, HistoryEntry]:
    """``{conversation_id: {"workspace", "display", "timestamp"}}`` from history.jsonl
    (interactive input log; last entry per id wins). Best-effort → ``{}``."""
    path = os.path.join(store_root(app_data_dir, home), "history.jsonl")
    idx: dict[str, HistoryEntry] = {}
    if not os.path.exists(path):
        return idx
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json_loads_object(line)
                except ValueError:
                    continue
                cid = obj.get("conversationId")
                if isinstance(cid, str) and cid:
                    workspace = obj.get("workspace")
                    idx[cid] = {"workspace": workspace if isinstance(workspace, str) else None,
                                "display": obj.get("display"),
                                "timestamp": obj.get("timestamp")}
    except OSError:
        pass
    return idx


# --- summaries index (used only when populated) ------------------------------
def _from_summaries(limit: int | None, app_data_dir: str | None, home: PathInput | None) -> list[ConversationInfo] | None:
    """Rows from ``conversation_summaries.db`` if it has any (it's empty on installs with a
    failed migration — then this returns ``None`` and the caller falls back to mtime scan)."""
    path = os.path.join(store_root(app_data_dir, home), "conversation_summaries.db")
    con = _ro_connect(path)
    if con is None:
        return None
    try:
        rows = cast(list[tuple[str, str | None, str | None, int | None, str | int | float | None, str | None, str | None]], con.execute(
            "select conversation_id, title, preview, step_count, last_modified_time, "
            "workspace_uris, status from conversation_summaries "
            "order by last_modified_time desc").fetchall())
    except sqlite3.Error:
        con.close()
        return None
    con.close()
    if not rows:
        return None
    out: list[ConversationInfo] = []
    for cid, title, preview, step_count, lmt, wuris, status in rows:
        out.append(ConversationInfo(
            id=cid, title=(title or preview or "")[:80], preview=preview or title or "",
            step_count=step_count, last_modified=_epoch(lmt), workspace=wuris,
            status=status, db=db_path(cid, app_data_dir, home)))
    return out[:limit] if limit else out


def _epoch(datetime_str: str | int | float | None) -> float:
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
def _all_db_paths(app_data_dir: str | None = None, home: PathInput | None = None) -> list[str]:
    return glob.glob(os.path.join(conversations_dir(app_data_dir, home), "*.db"))


def _safe_mtime(path: PathInput) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def list_conversations(limit: int | None = None, app_data_dir: str | None = None,
                       enrich: bool = True, home: PathInput | None = None) -> list[ConversationInfo]:
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
    out: list[ConversationInfo] = []
    for p in paths:
        cid = os.path.splitext(os.path.basename(p))[0]
        info = ConversationInfo(id=cid, last_modified=_safe_mtime(p), db=p)
        if enrich:
            info.step_count = _step_count(p)
            text = _first_user_text(cid, app_data_dir, home)
            info.title = text[:80]
            info.preview = text[:200]
            history = hist.get(cid)
            info.workspace = history["workspace"] if history else None
        out.append(info)
    return out


def latest_conversation_id(app_data_dir: str | None = None, home: PathInput | None = None) -> str | None:
    """The conversation ID with the newest ``conversations/*.db`` mtime, or ``None``."""
    paths = _all_db_paths(app_data_dir, home)
    if not paths:
        return None
    return os.path.splitext(os.path.basename(max(paths, key=_safe_mtime)))[0]


# --- resolving the ID a run just created / continued -------------------------
def snapshot(app_data_dir: str | None = None, home: PathInput | None = None) -> ConversationSnapshot:
    """Record ``{conversation_id: mtime}`` for every stored conversation *now*. Pass to
    :func:`capture_conversation_id` after a run to find which conversation it created (new
    id) or continued (mtime advanced)."""
    return {"mtimes": {os.path.splitext(os.path.basename(p))[0]: _safe_mtime(p)
                       for p in _all_db_paths(app_data_dir, home)}}


def _id_from_capture(capture_path: PathInput | None) -> str | None:
    """First ``{"kind":"conversation_id","id":...}`` event in the capture JSONL, or ``None``.
    Emitted by the file_open book (AGY_PROC_CONV_ID) from the conversation-store path — exact
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
                    obj = json_loads_object(line)
                except ValueError:
                    continue
                conversation_id = obj.get("id")
                if obj.get("kind") == "conversation_id" and isinstance(conversation_id, str) and conversation_id:
                    return conversation_id
    except OSError:
        return None
    return None


def capture_conversation_id(snapshot: ConversationSnapshot | None = None,
                            capture_path: PathInput | None = None, app_data_dir: str | None = None,
                            home: PathInput | None = None) -> str | None:
    """Resolve the conversation a just-finished run created or continued, most-reliable first:
      1. a ``conversation_id`` event from the file_open book in the capture JSONL (exact,
         in-process — needs AGY_PROC_CONV_ID + a linked run);
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


# --- write side: workspace trust + shared-auth/config store prep -------------
@contextmanager
def _settings_lock(root: PathInput) -> Generator[None]:
    os.makedirs(root, exist_ok=True)
    lock_path = os.path.join(root, ".settings.lock")
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield


def _read_json_obj(path: PathInput) -> JSONObject:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, errors="replace") as f:
            return json_object(json_load(f) or {})
    except (OSError, ValueError):
        return {}


def _atomic_write_json(path: str, data: JSONObject) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), mode)
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def trust_workspace(workspace: PathInput | None, home: PathInput | None = None,
                    app_data_dir: str | None = None) -> bool:
    """Add ``workspace`` (absolute) to ``trustedWorkspaces`` in the target store's
    ``settings.json`` — the same thing accepting agy's interactive "trust this folder" menu
    persists, so a live/interactive session won't block on it. Idempotent + atomic;
    best-effort (returns False on any error rather than raising)."""
    if not workspace:
        return False
    ws = os.path.abspath(workspace)
    root = store_root(app_data_dir, home)
    path = os.path.join(root, "settings.json")
    try:
        fast = _read_json_obj(path)
        if ws in json_array(fast.get("trustedWorkspaces") or []):
            return True
        with _settings_lock(root):
            data = _read_json_obj(path)
            tw = list(json_array(data.get("trustedWorkspaces") or []))
            if ws in tw:
                return True
            tw.append(ws)
            data["trustedWorkspaces"] = tw
            _atomic_write_json(path, data)
        return True
    except (OSError, ValueError):
        return False


def seed_onboarding(store_root_path: PathInput) -> str:
    """Mark a (scoped) agy store as already onboarded. Idempotent.

    agy's first-run wizard is not one of the prompts the PTY layer auto-answers
    (it answers terminal-capability and folder-trust queries only), so a fresh
    scoped home would hang there. Writing the onboarding cache up front makes
    the store look already-onboarded before agy starts."""
    cache = os.path.join(store_root_path, "cache")
    marker = os.path.join(cache, "onboarding.json")
    if not os.path.exists(marker):
        os.makedirs(cache, exist_ok=True)
        with open(marker, "w") as f:
            json.dump({"consumerOnboardingComplete": True,
                       "enterpriseOnboardingComplete": False,
                       "onboardingComplete": True}, f, indent=2)
    return marker


def _link_if_present(src: str, dst: str) -> None:
    if os.path.exists(src) and not os.path.lexists(dst):
        try:
            os.symlink(src, dst)
        except OSError:
            pass


def _sync_shared_auth_and_settings(real_app: str, root: str) -> None:
    """Seed shared login files + onboarding and live-sync global ``settings.json`` keys
    (everything except ``trustedWorkspaces``) from ``real_app`` into ``root``."""
    os.makedirs(root, exist_ok=True)
    for name in ("antigravity-oauth-token", "installation_id"):
        _link_if_present(os.path.join(real_app, name), os.path.join(root, name))
    seed_onboarding(root)
    real_cache = os.path.join(real_app, "cache")
    root_cache = os.path.join(root, "cache")
    for cache_name in ("default_project_id.txt", "projects.json"):
        _link_if_present(os.path.join(real_cache, cache_name), os.path.join(root_cache, cache_name))

    settings = os.path.join(root, "settings.json")
    global_settings = _read_json_obj(os.path.join(real_app, "settings.json"))
    local_settings = _read_json_obj(settings)
    desired: JSONObject = {k: v for k, v in global_settings.items() if k != "trustedWorkspaces"}
    desired["trustedWorkspaces"] = list(json_array(local_settings.get("trustedWorkspaces") or []))
    if os.path.isfile(settings) and not os.path.islink(settings) and local_settings == desired:
        return
    try:
        with _settings_lock(root):
            latest_local = _read_json_obj(settings)
            desired["trustedWorkspaces"] = list(
                json_array(latest_local.get("trustedWorkspaces") or []))
            if (os.path.isfile(settings) and not os.path.islink(settings)
                    and latest_local == desired):
                return
            _atomic_write_json(settings, desired)
    except OSError:
        pass


def _validate_app_data_dir(adir: str) -> str:
    parts = adir.split("/")
    if not adir or os.path.isabs(adir) or "." in parts or ".." in parts:
        raise ValueError(
            f"app_data_dir={adir!r} must be a clean relative subdirectory name under .gemini "
            "(use data_dir= to scope the entire .gemini tree to an arbitrary directory)")
    return adir


def prepare_app_data_dir(app_data_dir: str | None = None) -> str:
    """Prepare a dedicated ``~/.gemini/<app_data_dir>`` store (for ``agy --app_data_dir=...``)
    that isolates conversation/history files while sharing global ``~/.gemini/config``, login
    (OS keyring + symlinked ``antigravity-oauth-token`` / ``installation_id``), and global
    ``settings.json`` preferences (excluding ``trustedWorkspaces``). Idempotent."""
    adir = _validate_app_data_dir(resolve_app_data_dir(app_data_dir))
    real_gem = os.path.expanduser("~/.gemini")
    real_app = os.path.join(real_gem, GLOBAL_APP_DATA_DIR)
    root = os.path.join(real_gem, adir)
    if os.path.abspath(root) != os.path.abspath(real_app):
        _sync_shared_auth_and_settings(real_app, root)
    else:
        os.makedirs(root, exist_ok=True)
    return root


def prepare_scoped_home(data_dir: PathInput, app_data_dir: str | None = None,
                        link_global_config: bool = True) -> str:
    """Scope agy's data dir to ``data_dir`` (a project repo): a run launched with
    ``HOME=data_dir`` keeps its whole conversation store under ``data_dir/.gemini/`` instead
    of the global ``~/.gemini``. agy reads GeminiDir as ``$HOME/.gemini`` and looks for its
    login token there, so we **seed** the scoped tree with symlinks to the real
    ``antigravity-oauth-token`` + ``installation_id`` (login stays valid, and token refresh in
    the real store still applies) and the shared ``config/`` dir, and sync all global
    ``settings.json`` preferences (with a local ``trustedWorkspaces`` list). Idempotent.
    Returns the scoped store root (``data_dir/.gemini/<app_data_dir>``).

    ``link_global_config=False`` creates a REAL ``config/`` directory instead of
    symlinking the global one — for sessions that get their own MCP config
    (``mcp_config.json`` written into the scoped store) and must not observe or
    mutate the user's global servers. Because the symlink seeding is
    lexists-guarded, a later default-``True`` call (the launch-time
    ``scope_for_run``) leaves the real directory alone."""
    adir = _validate_app_data_dir(resolve_app_data_dir(app_data_dir, home=data_dir))
    real_gem = os.path.expanduser("~/.gemini")
    real_app = os.path.join(real_gem, GLOBAL_APP_DATA_DIR)
    gem = _gemini_dir(data_dir)                          # <data_dir>/.gemini
    root = store_root(adir, home=data_dir)               # <data_dir>/.gemini/<app_data_dir>
    os.makedirs(root, exist_ok=True)
    if link_global_config:
        _link_if_present(os.path.join(real_gem, "config"), os.path.join(gem, "config"))
    else:
        os.makedirs(os.path.join(gem, "config"), exist_ok=True)
    _sync_shared_auth_and_settings(real_app, root)
    return root


def scope_for_run(workspace: PathInput | None, data_dir: PathInput | None = None,
                  trust: bool = True, app_data_dir: str | None = None) -> tuple[PathInput | None, dict[str, str]]:
    """Prep native-store scoping + workspace trust for one run. If ``data_dir`` is set, seed a
    repo-scoped agy home (login + global settings preserved); otherwise prepare the dedicated
    ``~/.gemini/<app_data_dir>`` store (default ``~/.gemini/pyagy``). Pre-trust ``workspace`` in
    the target store's ``settings.json`` unless ``trust=False``. Returns ``(home, env_overrides)``."""
    home: PathInput | None = None
    env_overrides: dict[str, str] = {}
    adir = resolve_app_data_dir(app_data_dir, home=data_dir)
    if data_dir:
        prepare_scoped_home(data_dir, adir)
        home = data_dir
        env_overrides["HOME"] = os.path.abspath(data_dir)
    else:
        prepare_app_data_dir(adir)
    if trust:
        trust_workspace(workspace, home=home, app_data_dir=adir)
    return home, env_overrides
