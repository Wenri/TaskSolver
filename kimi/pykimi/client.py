"""Public pykimi client: ``ask`` / ``ask_many`` + ``KimiResponse``.

Runs the instrumented kimi-code (``kimi -p``) as a **wirecap mp-child** — the same
spawn/boot-channel machinery pyagy and pycodex use — and streams the decoded ``kimi_turn``s home
over a caller-owned ``SimpleQueue`` (:class:`pykimi.kimiprocess.KimiProcess`). The CLI's abrupt
one-shot exit can drop the last streamed turn, so the durable ``WIRE_CAPTURE`` JSONL the embedded
bridge writes stays **authoritative** for the returned turns, and the live stream is drained for
parity + as an fd-inheritance liveness probe (``n_streamed``). Mirrors ``pycodex.client``'s
``CodexResponse`` accessor shape (``.text`` / ``.primary`` / ``.model`` / ``.usage`` /
``.request``).
"""
import json
import os
import time
from dataclasses import dataclass

import multiprocessing.connection as _conn
from multiprocessing import get_context as _get_context

from wirecap.decode.mp_child import DONE as _DONE, EXC as _EXC   # result-queue completion sentinels
from wirecap.decode.turns import Usage, primary_turn, sum_usage   # noqa: F401 (Usage re-exported)
from wirecap.runtime.pty import answer_text as _clean_transcript
from wirecap.runtime.session import WireSession as _WireSession
from wirecap.runtime.workspace import ensure_git_workspace

from . import sessions as _sessions
from .kimiprocess import KimiProcess

_SPAWN = _get_context("spawn")    # context for the caller-owned result SimpleQueue


@dataclass
class KimiResponse:
    """The result of a kimi-code turn: the decoded ``kimi_turn``s + the stdout transcript
    fallback."""
    text: str
    transcript: str
    turns: list
    exit_status: int
    capture_path: str
    workspace: str
    n_streamed: int = 0   # kimi_turns that arrived over the LIVE queue (fd-inheritance probe; the
    #                       returned `turns` come from the authoritative capture JSONL, not this)
    timed_out: bool = False   # the drain deadline fired before the CLI exited (close() reaped it)
    session_id: str = None    # store-read after the run — never an echo of a requested resume id,
    #                           so a resume that silently forked a new session is visible here

    @property
    def primary(self):
        """The substantive model turn (most tokens) — kimi-code fires small side calls (titles,
        compaction) alongside the answer turn, so pick the max-token one. None if nothing
        decoded."""
        return primary_turn(self.turns)

    @property
    def request(self):
        p = self.primary
        return p.get("request") if p else None

    @property
    def model(self):
        p = self.primary
        if not p:
            return None
        r = p.get("request") or {}
        return r.get("model") or p.get("model")

    @property
    def usage(self):
        return sum_usage(self.turns)

    def __str__(self):
        return self.text


def _load_capture(path):
    """Return the decoded ``kimi_turn`` dicts from a capture JSONL (in file order)."""
    turns = []
    if not path or not os.path.exists(path):
        return turns
    with open(path) as f:
        for line in f:
            if '"kimi_turn"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("kind") == "kimi_turn":
                turns.append(obj)
    return turns


def _answer_text(turns, transcript):
    """The answer: the longest decoded turn text, else the PTY transcript (fallback).

    The transcript is FILTERED (wirecap.runtime.pty.answer_text) because our own bridge banner —
    "[wirecap/py] worker ready (… maxcopy=…)" — merges into it over the pty and would otherwise
    read as a plausible answer."""
    texts = [t.get("text") or "" for t in turns]
    best = max(texts, key=len) if texts else ""
    return best or _clean_transcript(transcript)


def _drain_stream(proc, q, timeout):
    """Drain ``kimi_turn`` dicts off the result queue until the CLI dies (its pidfd fires) / the
    target signals done / the reader EOFs / ``timeout``. Returns the streamed turns — a live bonus
    (the JSONL is authoritative); its length is the fd-inheritance liveness probe.

    The CLI runs under a pty, so this MUST keep the master drained (a full master buffer blocks
    it mid-write). ``service_pty`` does that in the same wait used to read results, and
    accumulates the transcript as a byproduct — the identical arrangement codex's drain uses."""
    reader = q._reader
    sentinel = getattr(proc._popen, "sentinel", None)
    watch = [reader] if sentinel is None else [reader, sentinel]
    turns, end, done = [], time.time() + timeout, False
    while not done and time.time() < end:
        slice_ = min(1.0, max(0.0, end - time.time()))
        proc.service_pty(slice_, watch)     # drains the pty; returns once a watched fd is ready
        ready = _conn.wait(watch, 0)
        try:
            while reader.poll(0):
                o = q.get()
                if isinstance(o, tuple) and o and o[0] in (_DONE, _EXC):
                    done = True
                elif isinstance(o, dict) and o.get("kind") == "kimi_turn":
                    turns.append(o)
        except EOFError:
            done = True                     # the CLI died and closed the queue writer
        if sentinel is not None and sentinel in ready:
            done = True                     # the CLI exited (the drain caught any buffered turns)
        elif sentinel is None and not ready and proc.reap():
            done = True                     # no pidfd: poll for death
    return turns, not done                  # not done = the deadline fired, the CLI was alive


def ask(prompt, *, model=None, workspace=None, timeout=300, extra_flags=None,
        kimi_bin=None, extra_env=None, mcp_servers=None, kimi_home=None,
        session_id=None, continue_latest=False, capture=True):
    """Run one instrumented ``kimi -p`` turn and return a :class:`KimiResponse`. The returned
    ``turns`` come from the authoritative capture JSONL; the live stream is drained for parity
    and probed via ``n_streamed``. Requires the built vendored bundle + addon (``pixi install``)
    and a model definition: either ``model=`` + ``KIMI_MODEL_*`` env via ``extra_env`` (the
    env-family route, no login needed), or the CLI's own ``kimi login``/config.

    ``session_id``/``continue_latest`` resume a stored session for the working directory
    (``-S <id>`` / ``--continue``) — check the returned ``KimiResponse.session_id`` (store-read,
    not an echo) to see the session actually continued. ``kimi_home`` scopes the whole store
    (config/sessions/wire journals) for the run and the post-run session-id read.
    ``mcp_servers`` ({name: {command, args, env}}) are written to the workspace-local
    ``<ws>/.kimi-code/mcp.json`` the CLI auto-discovers — kimi-code has no MCP flag — with the
    PYTHONHOME unwrap every spawned server needs (see :func:`pykimi.config.mcp_json`); a
    ``kimi_home``-scoped run can equally pre-write ``<home>/mcp.json`` itself and pass
    ``mcp_servers=None``. ``capture`` mirrors pyagy/pycodex: ``True`` writes
    ``<workspace>/kimi-capture.jsonl``; a path string keeps the capture out of the workspace.

    Approvals: print mode needs none. It forces ``auto`` permission and installs an
    auto-approving handler for the turn, and it *rejects* ``--yolo``/``--auto``/``--plan``
    (`Cannot combine --prompt with …`, exit 1, no output) — :func:`pykimi._env.kimi_argv`
    raises on those rather than letting the CLI fail silently. Those flags belong to
    shell-mode sessions."""
    if session_id and continue_latest:
        raise ValueError("session_id and continue_latest are mutually exclusive")
    ws = ensure_git_workspace(workspace)
    cap_path = capture if isinstance(capture, str) else os.path.join(ws, "kimi-capture.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(cap_path)), exist_ok=True)
    open(cap_path, "w").close()  # fresh capture per run: the bridge Recorder appends + the scratch
    #                              ws is reused across calls, so start clean
    q = _SPAWN.SimpleQueue()
    _write_mcp_json(ws, mcp_servers)
    proc = KimiProcess(prompt, workdir=ws, capture=cap_path, model=model,
                       extra_flags=extra_flags, kimi_bin=kimi_bin, extra_env=extra_env,
                       session_id=session_id, continue_latest=continue_latest,
                       kimi_home=kimi_home,
                       args=(q, ("kimi_turn",), timeout + 60))  # max_wait > timeout → death-based done
    proc.start()
    q._writer.close()            # parent only reads; the reader EOFs once the CLI (writer) dies
    try:
        streamed, timed_out = _drain_stream(proc, q, timeout)
    finally:
        proc.close()             # leader TERM + bounded reap + GROUP sweep: the CLI and its
        #                          MCP/shell children are never left running, exit_status set
        for c in (q._reader, q._writer):
            try:
                c.close()
            except Exception:
                pass
    turns = _load_capture(cap_path) or streamed  # JSONL authoritative; the stream is the fallback
    transcript = proc.transcript
    sid = _sessions.latest_session_id(home=kimi_home, cwd=ws) or session_id
    return KimiResponse(text=_answer_text(turns, transcript), transcript=transcript,
                        turns=turns, exit_status=proc.exit_status,
                        capture_path=cap_path, workspace=ws, n_streamed=len(streamed),
                        timed_out=timed_out, session_id=sid)


def _write_mcp_json(ws, mcp_servers):
    """Write the given servers to the workspace-local ``<ws>/.kimi-code/mcp.json`` the CLI
    auto-discovers (kimi-code takes MCP config from files, not flags; the project-local path is
    per-run scoped, so nothing global is mutated). Every command gets the PYTHONHOME unwrap —
    the CLI inherits this launcher's ``PYTHONHOME`` (for the embedded interpreter) and hands it
    to the MCP servers it spawns, which silently kills ``uv run`` venv pythons. A ``None`` leaves
    the workspace untouched (a caller-authored or home-scoped config stays in force)."""
    if not mcp_servers:
        return
    from .config import mcp_json
    cfg_dir = os.path.join(ws, ".kimi-code")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "mcp.json"), "w") as f:
        f.write(mcp_json(mcp_servers))


def ask_many(prompt, n, **kwargs):
    """Run ``n`` independent one-shot turns (sequentially). Same kwargs as :func:`ask`."""
    return [ask(prompt, **kwargs) for _ in range(n)]


class Session(_WireSession):
    """A multi-turn kimi-code session — the kimi counterpart of :class:`pyagy.Session` /
    :class:`pycodex.Session`, on the same :class:`wirecap.runtime.session.WireSession` base.

    In-run turns ride ONE live kimi-code shell UI (``kimi`` with no ``-p``); ``ask(prompt)``
    starts it on the first call and continues it thereafter. Shell mode takes no positional
    prompt, so the first turn is *typed* like every follow-up (``_PREFILL_FIRST = False``),
    multi-line prompts arriving as one bracketed paste (:meth:`KimiProcess.submit`). Across a
    restart, kimi's native store keeps context: pass ``session_id=`` (``-S <id>``) or
    ``continue_latest=True`` (``--continue``), or use :func:`resume` / :func:`continue_latest`.
    After the first turn :attr:`session_id` holds this session's id (store-read, never an echo)
    — persist it to resume later, and read :meth:`history` for the stored transcript.

    Before launch the workspace is pre-trusted in the session's store
    (:func:`pykimi.config.trust_workspace`) so the TUI's folder-trust gate never blocks;
    ``KimiPopen._answer`` accepts the dialog as the fallback. The turn boundary uses codex's
    idle numbers — kimi's bridge also emits the decoded turn at the response's terminal event,
    so an arriving turn is the real boundary and the timers are only the fallback.

    Use as a context manager to guarantee cleanup."""

    _KINDS = ("kimi_turn",)
    _IDLE = 8.0
    _PTY_IDLE = 25.0
    _PREFILL_FIRST = False    # shell mode: no argv prompt; turn 1 is typed

    def __init__(self, *, model=None, workspace=None, timeout=300, idle=8.0,
                 kimi_bin=None, extra_env=None, session_id=None, continue_latest=False,
                 extra_flags=None, mcp_servers=None, kimi_home=None, capture_tail=False):
        if session_id and continue_latest:
            raise ValueError("session_id and continue_latest are mutually exclusive")
        self.workspace = ensure_git_workspace(workspace)
        self.model = model
        self.timeout = timeout
        self.idle = idle
        self.capture_tail = capture_tail
        self.kimi_bin = kimi_bin
        self.extra_env = extra_env
        self.kimi_home = kimi_home            # scope the store; also the id/history read root
        self.extra_flags = extra_flags
        self.continue_latest = continue_latest
        self._mcp_servers = mcp_servers
        # NOT ask()'s "kimi-capture.jsonl": a Session sharing a workspace with
        # one-shot calls must not interleave two writers into one capture file.
        self.cap_path = os.path.join(self.workspace, "kimi-session.jsonl")
        self._id = session_id
        self._cap_seen = 0                    # capture_tail turn-count cursor

    def _pre_start(self):
        open(self.cap_path, "w").close()      # fresh capture for this session
        _write_mcp_json(self.workspace, self._mcp_servers)
        from .config import trust_workspace
        trust_workspace(self.workspace, home=self.kimi_home)

    def _make_process(self, prompt):
        return KimiProcess(prompt, persistent=True, workdir=self.workspace,
                           capture=self.cap_path, model=self.model,
                           kimi_bin=self.kimi_bin, extra_env=self.extra_env,
                           extra_flags=self.extra_flags, session_id=self._id,
                           continue_latest=self.continue_latest,
                           kimi_home=self.kimi_home,
                           args=(self._q, ("kimi_turn",), None))  # max_wait=None: base asserts

    def _latch_id(self):
        return _sessions.latest_session_id(home=self.kimi_home, cwd=self.workspace)

    def _build_response(self, objs, reason):
        transcript = self._proc.transcript
        return KimiResponse(
            text=_answer_text(objs, transcript), transcript=transcript, turns=objs,
            exit_status=self._proc.exit_status, capture_path=self.cap_path,
            workspace=self.workspace, n_streamed=len(objs),
            timed_out=(reason == "deadline"), session_id=self._id)

    def _read_history(self):
        """The stored transcript, projected from the session's wire journals — a list of
        ``{step_index, role, type, created_at, content}`` (see
        :func:`pykimi.sessions.read_transcript`)."""
        return _sessions.read_transcript(self._id, home=self.kimi_home)

    def _load_capture_tail(self):
        """This turn's ``kimi_turn`` records from the cumulative session capture:
        everything decoded past the previous turn's cursor."""
        turns = _load_capture(self.cap_path)
        fresh = turns[self._cap_seen:]
        self._cap_seen = len(turns)
        return fresh


def resume(session_id, **kwargs):
    """A :class:`Session` that resumes the stored session ``session_id`` (``kimi -S <id>``).
    Its first ``.ask()`` continues that session with full prior context — even in a brand-new
    process. ``**kwargs`` are :class:`Session`'s."""
    return Session(session_id=session_id, **kwargs)


def continue_latest(**kwargs):
    """A :class:`Session` resuming the working directory's most recent stored session
    (``kimi --continue``)."""
    return Session(continue_latest=True, **kwargs)
