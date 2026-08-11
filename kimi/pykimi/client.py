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

    Approvals: print mode has no interactive prompt — pass ``extra_flags=["--yolo"]`` or provide
    permission rules in the run's config for tool-using tasks."""
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
