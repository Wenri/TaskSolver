"""Public pycodex client: ``ask`` / ``ask_many`` + ``CodexResponse``.

Runs the instrumented codex (``codex exec``) as a **wirecap mp-child** — the same spawn/boot-channel
machinery pyagy uses — and streams the decoded ``codex_turn``s home over a caller-owned
``SimpleQueue`` (:class:`pycodex.codexprocess.CodexProcess`). codex emits no terminal signal to
Python and ``codex exec`` is a one-shot, so its abrupt exit can drop the last streamed turn;
therefore the durable ``WIRE_CAPTURE`` JSONL the embedded bridge writes stays **authoritative** for
the returned turns, and the live stream is drained for parity with agy + as an fd-inheritance
liveness probe (``n_streamed``). Mirrors ``pyagy.client``'s ``AgyResponse`` accessor shape
(``.text`` / ``.primary`` / ``.model`` / ``.usage`` / ``.request``).
"""
import json
import os
import time
from dataclasses import dataclass, field

import multiprocessing.connection as _conn
from multiprocessing import get_context as _get_context

from wirecap.decode.mp_child import DONE as _DONE, EXC as _EXC   # result-queue completion sentinels
from wirecap.decode.turns import Usage, primary_turn, sum_usage   # noqa: F401 (Usage re-exported)
from wirecap.runtime.pty import answer_text as _clean_transcript
from wirecap.runtime.workspace import ensure_git_workspace

from . import sessions as _sessions
from .codexprocess import CodexProcess

_SPAWN = _get_context("spawn")    # context for the caller-owned result SimpleQueue




@dataclass
class CodexResponse:
    """The result of a codex turn: the decoded ``codex_turn``s + the stdout transcript fallback."""
    text: str
    transcript: str
    turns: list
    exit_status: int
    capture_path: str
    workspace: str
    n_streamed: int = 0   # codex_turns that arrived over the LIVE queue (fd-inheritance probe; the
    #                       returned `turns` come from the authoritative capture JSONL, not this)
    timed_out: bool = False   # the drain deadline fired before codex exited (close() then reaped it)
    session_id: str = None    # store-read after the run — never an echo of a requested resume id,
    #                           so a resume that silently forked a new thread is visible here

    @property
    def primary(self):
        """The substantive model turn (most tokens) — codex runs a small secondary call per exec
        alongside the answer turn, so pick the max-token one. None if nothing decoded."""
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
    """Return the decoded ``codex_turn`` dicts from a capture JSONL (in file order)."""
    turns = []
    if not path or not os.path.exists(path):
        return turns
    with open(path) as f:
        for line in f:
            if '"codex_turn"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("kind") == "codex_turn":
                turns.append(obj)
    return turns


def _answer_text(turns, transcript):
    """The answer: the longest decoded turn text, else the PTY transcript (fallback).

    The transcript is FILTERED (wirecap.runtime.pty.answer_text) because codex's stderr — which
    carries our own bridge banner, e.g. "[wirecap/py] worker ready (… maxcopy=1048576)" — merges
    into it over the pty. Unfiltered, that banner is a plausible-looking answer: it satisfied a
    "reply with only the number" parse with 1048576."""
    texts = [t.get("text") or "" for t in turns]
    best = max(texts, key=len) if texts else ""
    return best or _clean_transcript(transcript)


def _drain_stream(proc, q, timeout):
    """Drain ``codex_turn`` dicts off the result queue until codex dies (its pidfd fires) / the
    target signals done / the reader EOFs / ``timeout``. Returns the streamed turns — a live bonus
    (the JSONL is authoritative); its length is the fd-inheritance liveness probe.

    codex runs under a pty, so this MUST keep the master drained (a full master buffer blocks codex
    mid-write). ``service_pty`` does that in the same wait used to read results, and accumulates the
    transcript as a byproduct — the identical arrangement agy's ``_collect`` uses."""
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
                elif isinstance(o, dict) and o.get("kind") == "codex_turn":
                    turns.append(o)
        except EOFError:
            done = True                     # codex died and closed the queue writer
        if sentinel is not None and sentinel in ready:
            done = True                     # codex exited (the drain above caught any buffered turns)
        elif sentinel is None and not ready and proc.reap():
            done = True                     # no pidfd: poll for death
    return turns, not done                  # not done = the deadline fired, codex was still alive


def _mcp_flags(mcp_servers, extra_flags):
    """Prepend rendered ``-c mcp_servers.<name>=...`` flags for the given servers
    (see :func:`pycodex.config.mcp_flags` — every command gets the PYTHONHOME
    unwrap, because codex inherits this launcher's ``PYTHONHOME`` and hands it to
    the MCP servers it spawns) to the caller's extra_flags."""
    if not mcp_servers:
        return extra_flags
    from .config import mcp_flags
    return [*mcp_flags(mcp_servers), *(extra_flags or [])]


def ask(prompt, *, model=None, workspace=None, timeout=300, extra_flags=None,
        codex_bin=None, extra_env=None, mcp_servers=None, codex_home=None,
        session_id=None, continue_latest=False, prompt_via_stdin=False,
        capture=True):
    """Run one instrumented ``codex exec`` turn and return a :class:`CodexResponse`. The returned
    ``turns`` come from the authoritative capture JSONL; the live stream is drained for parity and
    probed via ``n_streamed``. Requires the built, wirecap-patched codex and codex auth
    (``OPENAI_API_KEY`` or ``codex login``).

    ``session_id``/``continue_latest`` are the NON-interactive resume (``codex exec resume``) —
    beware codex silently starts a NEW thread when the id does not resolve in the store, so check
    the returned ``CodexResponse.session_id`` (store-read, not an echo). ``codex_home`` scopes the
    whole store (auth/sessions/rollouts) for the run and the post-run session-id read.
    ``prompt_via_stdin`` delivers the prompt on fd 0 (``codex exec -``) — no quoting or ARG_MAX
    limits, nothing echoed into the transcript. ``capture`` mirrors pyagy: ``True`` writes
    ``<workspace>/codex-capture.jsonl``; a path string keeps the capture out of the workspace.

    To run with seeded ``AGENTS.md`` instructions or skills, seed a workspace with
    ``ensure_git_workspace(...)`` and pass it as ``workspace=`` — omitting it resolves the shared
    scratch repo with no seeds, which *clears* any a previous call left there (seeding never
    writes into a caller-supplied workspace unless instructions/skills are given)."""
    if session_id and continue_latest:
        raise ValueError("session_id and continue_latest are mutually exclusive")
    ws = ensure_git_workspace(workspace)
    cap_path = capture if isinstance(capture, str) else os.path.join(ws, "codex-capture.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(cap_path)), exist_ok=True)
    open(cap_path, "w").close()  # fresh capture per run: the bridge Recorder appends + the scratch ws
    #                              is reused across calls, so start clean (also the no-stream fallback)
    q = _SPAWN.SimpleQueue()
    extra_flags = _mcp_flags(mcp_servers, extra_flags)
    proc = CodexProcess(prompt, workdir=ws, capture=cap_path, model=model,
                        extra_flags=extra_flags, codex_bin=codex_bin, extra_env=extra_env,
                        session_id=session_id, continue_latest=continue_latest,
                        codex_home=codex_home, prompt_via_stdin=prompt_via_stdin,
                        args=(q, ("codex_turn",), timeout + 60))  # max_wait > timeout → death-based done
    proc.start()
    q._writer.close()            # parent only reads; the reader EOFs once codex (the writer holder) dies
    try:
        streamed, timed_out = _drain_stream(proc, q, timeout)
    finally:
        proc.close()             # leader TERM + bounded reap + GROUP sweep: codex and its MCP/tool
        #                          children are never left running, exit_status set
        for c in (q._reader, q._writer):
            try:
                c.close()
            except Exception:
                pass
    turns = _load_capture(cap_path) or streamed  # JSONL authoritative; the stream is the fallback
    transcript = proc.transcript
    sid = _sessions.latest_session_id(home=codex_home, cwd=ws) or session_id
    return CodexResponse(text=_answer_text(turns, transcript), transcript=transcript,
                         turns=turns, exit_status=proc.exit_status,
                         capture_path=cap_path, workspace=ws, n_streamed=len(streamed),
                         timed_out=timed_out, session_id=sid)


def _ask_turn(proc, q, prompt=None, idle=8.0, pty_idle=25.0, timeout=180.0, ready=2.5):
    """One turn against a LIVE interactive codex: submit `prompt`, then drain decoded
    ``codex_turn``s until the turn settles.

    Unlike agy's equivalent this does not have to guess from PTY quiet alone: the bridge emits a
    decoded turn at the response's terminal event, so a turn ARRIVING is the real boundary and the
    idle timers are only the fallback for a turn that never decodes (auth/spend-cap/tool-only
    reply). `ready` waits for the TUI to settle before typing, so the prompt is not swallowed."""
    reader = q._reader
    rstart = time.time()
    while time.time() - rstart < 30 and time.time() - proc.last_output < ready:
        proc.service_pty(0.2, [reader])
    if prompt is None:
        proc.write(b"\r")                     # prefill already on the argv: just submit
    else:
        proc.send_line(prompt)
    proc.last_output = time.time()
    got, last, start = [], None, time.time()
    while time.time() - start < timeout:
        if proc.service_pty(0.2, [reader]):
            while reader.poll(0):
                try:
                    o = q.get()
                except EOFError:
                    proc.reap()                # codex exited — nothing more will arrive
                    return got
                if isinstance(o, tuple) and o and o[0] in (_DONE, _EXC):
                    proc.reap()                # the in-codex target finished/raised
                    return got
                if isinstance(o, dict) and o.get("kind") == "codex_turn":
                    got.append(o)
                    last = time.time()
        now = time.time()
        if last is not None and now - last >= idle:
            break                              # a turn decoded and the stream went quiet
        if last is None and now - proc.last_output >= pty_idle:
            break                              # codex went idle without producing a turn
    return got


class Session:
    """A multi-turn codex session — the codex counterpart of :class:`pyagy.Session`.

    In-run turns ride ONE live interactive codex process (bare ``codex``, the TUI);
    ``ask(prompt)`` starts it on the first call and continues it thereafter. Across a restart,
    codex's own rollout store keeps context: pass ``session_id=`` (resume a specific stored
    session) or ``continue_latest=True`` (resume the newest), or use the module helpers
    :func:`resume` / :func:`continue_latest`. After the first turn :attr:`session_id` holds this
    session's id — persist it to resume later, and read :meth:`history` for the stored transcript.

    Use as a context manager to guarantee cleanup. (There is no ``set_rewrite`` here: the SYNC
    egress rewrite is a property of agy's LD_PRELOAD shim, and codex has no such hook.)"""

    def __init__(self, *, model=None, workspace=None, timeout=180, idle=8.0,
                 codex_bin=None, extra_env=None, session_id=None, continue_latest=False,
                 extra_flags=None, mcp_servers=None, codex_home=None):
        self.workspace = ensure_git_workspace(workspace)
        self.model = model
        self.timeout = timeout
        self.idle = idle
        self.codex_bin = codex_bin
        self.extra_env = extra_env
        self.codex_home = codex_home          # scope the store; also used for the id/history reads
        self.extra_flags = _mcp_flags(mcp_servers, extra_flags)
        self.continue_latest = continue_latest
        self.cap_path = os.path.join(self.workspace, "codex-capture.jsonl")
        self._session_id = session_id
        self._codex = None
        self._q = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _start(self, prompt):
        open(self.cap_path, "w").close()      # fresh capture for this session
        self._q = _SPAWN.SimpleQueue()
        self._codex = CodexProcess(
            prompt, persistent=True, workdir=self.workspace, capture=self.cap_path,
            model=self.model, codex_bin=self.codex_bin, extra_env=self.extra_env,
            extra_flags=self.extra_flags, session_id=self._session_id,
            continue_latest=self.continue_latest, codex_home=self.codex_home,
            # no deadline: a Session's life IS codex's life (caller think-time between turns
            # is unbounded). close()/GC kill codex, which ends the in-codex target.
            args=(self._q, ("codex_turn",), None))
        self._codex.start()
        self._q._writer.close()               # parent reads only; reader EOFs on codex death

    def ask(self, prompt):
        """Send ``prompt`` (starting the session on first call) and return the
        :class:`CodexResponse` for the turn it produced."""
        if self._codex is not None and self._codex.exit_status is not None:
            raise RuntimeError(
                f"this Session's codex process already exited "
                f"(exit_status={self._codex.exit_status}); open a new Session "
                f"(resume with session_id={self._session_id!r})")
        if self._codex is None:
            self._start(prompt)
            turns = _ask_turn(self._codex, self._q, None, idle=self.idle, timeout=self.timeout)
        else:
            turns = _ask_turn(self._codex, self._q, prompt, idle=self.idle, timeout=self.timeout)
        transcript = self._codex.transcript
        if self._session_id is None:          # first turn of a fresh session
            self._session_id = _sessions.latest_session_id(home=self.codex_home,
                                                           cwd=self.workspace)
        return CodexResponse(
            text=_answer_text(turns, transcript), transcript=transcript, turns=turns,
            exit_status=self._codex.exit_status, capture_path=self.cap_path,
            workspace=self.workspace, n_streamed=len(turns))

    @property
    def session_id(self):
        """codex's native session id — the resumed id, or the one captured after the first turn.
        Persist it and pass to :func:`resume` to continue this session in a later process."""
        return self._session_id

    #: pyagy spells this ``conversation_id``; alias it so cross-provider code can read one name.
    conversation_id = session_id

    def history(self):
        """The stored transcript for this session, read from codex's own rollout store — a list
        of ``{step_index, role, type, created_at, content}`` (see
        :func:`pycodex.sessions.read_transcript`). Empty until an id is known."""
        if not self._session_id:
            return []
        return _sessions.read_transcript(self._session_id, home=self.codex_home)

    def close(self):
        try:
            if self._codex is not None:
                self._codex.close(interrupt=True)
        finally:
            if self._q is not None:
                for c in (self._q._reader, self._q._writer):
                    try:
                        c.close()
                    except Exception:
                        pass


# --- session entry points (mirrors pyagy.resume / pyagy.continue_latest) -----
def resume(session_id, **kwargs):
    """A :class:`Session` continuing the stored codex session ``session_id``."""
    return Session(session_id=session_id, **kwargs)


def continue_latest(**kwargs):
    """A :class:`Session` continuing the most recent stored codex session."""
    return Session(continue_latest=True, **kwargs)


def ask_many(prompt, n, **kwargs):
    """Run ``n`` independent one-shot turns (sequentially). Same kwargs as :func:`ask`."""
    return [ask(prompt, **kwargs) for _ in range(n)]
