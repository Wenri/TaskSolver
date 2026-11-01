"""Public pycodex client: ``ask`` / ``ask_many`` + ``CodexResponse``.

Runs the linked codex (``codex exec``) as a **wirecap mp-child** — the same spawn/boot-channel
machinery pyagy uses — and streams the decoded ``codex_turn``s home over a caller-owned
``SimpleQueue`` (:class:`pycodex.codexprocess.CodexProcess`). codex emits no terminal signal to
Python and ``codex exec`` is a one-shot, so its abrupt exit can drop the last streamed turn;
therefore the durable ``WIRE_CAPTURE`` JSONL the embedded bridge writes stays **authoritative** for
the returned turns, and the live stream is drained for parity with agy + as an fd-inheritance
liveness probe (``n_streamed``). Mirrors ``pyagy.client``'s ``AgyResponse`` accessor shape
(``.text`` / ``.primary`` / ``.model`` / ``.usage`` / ``.request``).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import TypedDict, Unpack, cast

import multiprocessing.connection as _conn

from wirecap.decode.mp_child import DONE as _DONE, EXC as _EXC   # result-queue completion sentinels
from wirecap.decode.turns import Usage as Usage, primary_turn, sum_usage
from wirecap.runtime import session as _wire_session
from wirecap.runtime.pty import answer_text as _clean_transcript
from wirecap.runtime.session import (WireSession as _WireSession, ResultChannel, TurnReason, new_channel)
from wirecap.runtime.mcp import MCPServers
from wirecap.types import Event, json_loads_object
from wirecap.runtime.workspace import ensure_git_workspace

from . import sessions as _sessions
from .codexprocess import CodexProcess

__all__ = ["ask", "ask_many", "resume", "continue_latest", "Session", "CodexResponse", "Usage", "_ask_turn"]


class ClientOptions(TypedDict, total=False):
    model: str | None
    workspace: str | None
    timeout: float
    extra_flags: Sequence[str] | None
    codex_bin: str | None
    extra_env: Mapping[str, str] | None
    mcp_servers: MCPServers | None
    codex_home: str | None


class AskOptions(ClientOptions, total=False):
    session_id: str | None
    continue_latest: bool
    capture: bool | str
    prompt_via_stdin: bool


class SessionDefaults(ClientOptions, total=False):
    idle: float
    capture_tail: bool


class SessionOptions(SessionDefaults, total=False):
    session_id: str | None
    continue_latest: bool




@dataclass
class CodexResponse:
    """The result of a codex turn: the decoded ``codex_turn``s + the stdout transcript fallback."""
    text: str
    transcript: str
    turns: list[Event]
    exit_status: int | None
    capture_path: str
    workspace: str
    n_streamed: int = 0   # codex_turns that arrived over the LIVE queue (fd-inheritance probe; the
    #                       returned `turns` come from the authoritative capture JSONL, not this)
    timed_out: bool = False   # the drain deadline fired before codex exited (close() then reaped it)
    session_id: str | None = None    # store-read after the run — never an echo of a requested resume id,
    #                           so a resume that silently forked a new thread is visible here

    @property
    def primary(self) -> Event | None:
        """The substantive model turn (most tokens) — codex runs a small secondary call per exec
        alongside the answer turn, so pick the max-token one. None if nothing decoded."""
        return primary_turn(self.turns)

    @property
    def request(self) -> Mapping[str, object] | None:
        p = self.primary
        request = p.get("request") if p else None
        return cast(Mapping[str, object], request) if isinstance(request, Mapping) else None

    @property
    def model(self) -> str | None:
        p = self.primary
        if not p:
            return None
        request = self.request
        model = (request.get("model") if request else None) or p.get("model")
        return model if isinstance(model, str) else None

    @property
    def usage(self) -> Usage:
        return sum_usage(self.turns)

    def __str__(self) -> str:
        return self.text


def _load_capture(path: str | None) -> list[Event]:
    """Return the decoded ``codex_turn`` dicts from a capture JSONL (in file order)."""
    turns: list[Event] = []
    if not path or not os.path.exists(path):
        return turns
    with open(path) as f:
        for line in f:
            if '"codex_turn"' not in line:
                continue
            try:
                obj: Event = dict(json_loads_object(line))
            except ValueError:
                continue
            if obj.get("kind") == "codex_turn":
                turns.append(obj)
    return turns


def _answer_text(turns: Sequence[Mapping[str, object]], transcript: str) -> str:
    """The answer: the longest decoded turn text, else the PTY transcript (fallback).

    The transcript is FILTERED (wirecap.runtime.pty.answer_text) because codex's stderr — which
    carries our own bridge banner, e.g. "[wirecap/py] worker ready (… maxcopy=1048576)" — merges
    into it over the pty. Unfiltered, that banner is a plausible-looking answer: it satisfied a
    "reply with only the number" parse with 1048576."""
    texts = [text for t in turns if isinstance(text := t.get("text"), str)]
    best = max(texts, key=len) if texts else ""
    return best or _clean_transcript(transcript)


def _drain_stream(proc: CodexProcess, q: ResultChannel, timeout: float) -> tuple[list[Event], bool]:
    """Drain ``codex_turn`` dicts off the result queue until codex dies (its pidfd fires) / the
    target signals done / the reader EOFs / ``timeout``. Returns the streamed turns — a live bonus
    (the JSONL is authoritative); its length is the fd-inheritance liveness probe.

    codex runs under a pty, so this MUST keep the master drained (a full master buffer blocks codex
    mid-write). ``service_pty`` does that in the same wait used to read results, and accumulates the
    transcript as a byproduct — the identical arrangement agy's ``_collect`` uses."""
    reader = q._reader
    popen = proc._popen
    sentinel = popen.death_sentinel if popen is not None else None
    watch = [reader] if sentinel is None else [reader, sentinel]
    turns: list[Event] = []
    end, done = time.time() + timeout, False
    while not done and time.time() < end:
        slice_ = min(1.0, max(0.0, end - time.time()))
        proc.service_pty(slice_, watch)     # drains the pty; returns once a watched fd is ready
        ready = _conn.wait(watch, 0)
        try:
            while reader.poll(0):
                value = q.get()
                if isinstance(value, tuple):
                    signal = cast(tuple[object, ...], value)
                    if signal and signal[0] in (_DONE, _EXC):
                        done = True
                elif isinstance(value, dict):
                    event = cast(Event, value)
                    if event.get("kind") == "codex_turn":
                        turns.append(event)
        except EOFError:
            done = True                     # codex died and closed the queue writer
        if sentinel is not None and sentinel in ready:
            done = True                     # codex exited (the drain above caught any buffered turns)
        elif sentinel is None and not ready and proc.reap():
            done = True                     # no pidfd: poll for death
    return turns, not done                  # not done = the deadline fired, codex was still alive


def _mcp_flags(mcp_servers: MCPServers | None, extra_flags: Sequence[str] | None) -> Sequence[str] | None:
    """Prepend rendered ``-c mcp_servers.<name>=...`` flags for the given servers
    (see :func:`pycodex.config.mcp_flags` — every command gets the PYTHONHOME
    unwrap, because codex inherits this launcher's ``PYTHONHOME`` and hands it to
    the MCP servers it spawns) to the caller's extra_flags."""
    if not mcp_servers:
        return extra_flags
    from .config import mcp_flags
    return [*mcp_flags(mcp_servers), *(extra_flags or [])]


def ask(prompt: str, *, model: str | None = None, workspace: str | None = None,
        timeout: float = 300, extra_flags: Sequence[str] | None = None,
        codex_bin: str | None = None, extra_env: Mapping[str, str] | None = None,
        mcp_servers: MCPServers | None = None, codex_home: str | None = None,
        session_id: str | None = None, continue_latest: bool = False,
        prompt_via_stdin: bool = False, capture: bool | str = True) -> CodexResponse:
    """Run one linked ``codex exec`` turn and return a :class:`CodexResponse`. The returned
    ``turns`` come from the authoritative capture JSONL; the live stream is drained for parity and
    probed via ``n_streamed``. Requires the built, wirecap-patched codex and codex auth
    (``OPENAI_API_KEY`` or ``codex login``).

    ``session_id``/``continue_latest`` are the NON-interactive resume (``codex exec resume``) —
    beware codex silently starts a NEW thread when the id does not resolve in the store, so check
    the returned ``CodexResponse.session_id`` (store-read, not an echo). ``codex_home`` scopes the
    whole store (auth/sessions/rollouts) for the run and the post-run session-id read.
    ``prompt_via_stdin`` delivers the prompt on fd 0 (``codex exec -``) — no quoting or ARG_MAX
    limits, nothing echoed into the transcript. ``capture`` mirrors pyagy: ``True`` writes
    ``<workspace>/codex-capture.jsonl``; a path string selects its location, and ``False``
    disables the capture file while retaining live streamed turns.

    To run with seeded ``AGENTS.md`` instructions or skills, seed a workspace with
    ``ensure_git_workspace(...)`` and pass it as ``workspace=`` — omitting it resolves the shared
    scratch repo with no seeds, which *clears* any a previous call left there (seeding never
    writes into a caller-supplied workspace unless instructions/skills are given)."""
    if session_id and continue_latest:
        raise ValueError("session_id and continue_latest are mutually exclusive")
    ws = ensure_git_workspace(workspace)
    cap_path = (capture if isinstance(capture, str) else os.path.join(ws, "codex-capture.jsonl")) if capture else ""
    if cap_path:
        os.makedirs(os.path.dirname(os.path.abspath(cap_path)), exist_ok=True)
        open(cap_path, "w").close()  # Recorder appends; each call starts a fresh capture.
    q = new_channel()
    extra_flags = _mcp_flags(mcp_servers, extra_flags)
    proc = CodexProcess(prompt, workdir=ws, capture=cap_path or os.devnull, model=model,
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


def _ask_turn(proc: CodexProcess, q: ResultChannel, prompt: str | None = None,
              idle: float = 8.0, pty_idle: float = 25.0, timeout: float = 180.0,
              ready: float = 2.5) -> list[Event]:
    """One turn against a LIVE interactive codex: submit `prompt`, then drain decoded
    ``codex_turn``s until the turn settles.

    Unlike agy's equivalent this does not have to guess from PTY quiet alone: the bridge emits a
    decoded turn at the response's terminal event, so a turn ARRIVING is the real boundary and the
    idle timers are only the fallback for a turn that never decodes (auth/spend-cap/tool-only
    reply). Compat shim over :func:`wirecap.runtime.session.ask_turn`, keeping this module's
    historical defaults and objs-only return."""
    got, _reason = _wire_session.ask_turn(proc, q, prompt, kinds=("codex_turn",), idle=idle,
                                          pty_idle=pty_idle, timeout=timeout, ready=ready)
    return got


class Session(_WireSession[CodexResponse, CodexProcess]):
    """A multi-turn codex session — the codex counterpart of :class:`pyagy.Session`.

    In-run turns ride ONE live interactive codex process (bare ``codex``, the TUI);
    ``ask(prompt)`` starts it on the first call and continues it thereafter. Across a restart,
    codex's own rollout store keeps context: pass ``session_id=`` (resume a specific stored
    session) or ``continue_latest=True`` (resume the newest), or use the module helpers
    :func:`resume` / :func:`continue_latest`. After the first turn :attr:`session_id` holds this
    session's id — persist it to resume later, and read :meth:`history` for the stored transcript.

    Use as a context manager to guarantee cleanup. (There is no ``set_rewrite`` here: the SYNC
    egress rewrite belongs to agy's tls_write book; Codex has no equivalent capture point.)
    Lifecycle lives on :class:`wirecap.runtime.session.WireSession`; this class supplies codex's
    process construction and response shape."""

    _KINDS = ("codex_turn",)
    _IDLE = 8.0
    _PTY_IDLE = 25.0

    def __init__(self, *, model: str | None = None, workspace: str | None = None,
                 timeout: float = 180, idle: float = 8.0,
                 codex_bin: str | None = None, extra_env: Mapping[str, str] | None = None,
                 session_id: str | None = None, continue_latest: bool = False,
                 extra_flags: Sequence[str] | None = None, mcp_servers: MCPServers | None = None,
                 codex_home: str | None = None, capture_tail: bool = False) -> None:
        self.workspace = ensure_git_workspace(workspace)
        self.model = model
        self.timeout = timeout
        self.idle = idle
        self.capture_tail = capture_tail
        self.codex_bin = codex_bin
        self.extra_env = extra_env
        self.codex_home = codex_home          # scope the store; also used for the id/history reads
        self.extra_flags = _mcp_flags(mcp_servers, extra_flags)
        self.continue_latest = continue_latest
        # NOT ask()'s "codex-capture.jsonl": a Session sharing a workspace with
        # one-shot calls must not interleave two writers into one capture file
        # (pyagy's session capture is separate for the same reason).
        self.cap_path = os.path.join(self.workspace, "codex-session.jsonl")
        self._id = session_id
        self._cap_seen = 0                    # capture_tail turn-count cursor

    def _pre_start(self) -> None:
        open(self.cap_path, "w").close()      # fresh capture for this session

    def _make_process(self, prompt: str | None) -> CodexProcess:
        return CodexProcess(
            prompt, persistent=True, workdir=self.workspace, capture=self.cap_path,
            model=self.model, codex_bin=self.codex_bin, extra_env=self.extra_env,
            extra_flags=self.extra_flags, session_id=self._id,
            continue_latest=self.continue_latest, codex_home=self.codex_home,
            args=(self._q, ("codex_turn",), None))  # max_wait=None: base asserts

    def _latch_id(self) -> str | None:
        return _sessions.latest_session_id(home=self.codex_home, cwd=self.workspace)

    def _build_response(self, objs: list[Event], reason: TurnReason) -> CodexResponse:
        assert self._proc is not None
        transcript = self._proc.transcript
        return CodexResponse(
            text=_answer_text(objs, transcript), transcript=transcript, turns=objs,
            exit_status=self._proc.exit_status, capture_path=self.cap_path,
            workspace=self.workspace, n_streamed=len(objs),
            timed_out=(reason == "deadline"), session_id=self._id)

    def _read_history(self) -> list[Mapping[str, object]]:
        """The stored transcript for this session, read from codex's own rollout store — a list
        of ``{step_index, role, type, created_at, content}`` (see
        :func:`pycodex.sessions.read_transcript`)."""
        return list(_sessions.read_transcript(self._id, home=self.codex_home))

    def _load_capture_tail(self) -> list[Event]:
        """This turn's ``codex_turn`` records from the cumulative session capture:
        everything decoded past the previous turn's cursor."""
        turns = _load_capture(self.cap_path)
        fresh = turns[self._cap_seen:]
        self._cap_seen = len(turns)
        return fresh


# --- session entry points (mirrors pyagy.resume / pyagy.continue_latest) -----
def resume(session_id: str, **kwargs: Unpack[SessionDefaults]) -> Session:
    """A :class:`Session` continuing the stored codex session ``session_id``."""
    return Session(session_id=session_id, **kwargs)


def continue_latest(**kwargs: Unpack[SessionDefaults]) -> Session:
    """A :class:`Session` continuing the most recent stored codex session."""
    return Session(continue_latest=True, **kwargs)


def ask_many(prompt: str, n: int, **kwargs: Unpack[AskOptions]) -> list[CodexResponse]:
    """Run ``n`` independent one-shot turns (sequentially). Same kwargs as :func:`ask`."""
    return [ask(prompt, **kwargs) for _ in range(n)]
