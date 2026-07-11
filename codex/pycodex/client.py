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
from wirecap.runtime.workspace import ensure_git_workspace

from .codexprocess import CodexProcess

_SPAWN = _get_context("spawn")    # context for the caller-owned result SimpleQueue


@dataclass
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    raw: dict = field(default_factory=dict)


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

    @property
    def primary(self):
        """The substantive model turn (most tokens) — codex runs a small secondary call per exec
        alongside the answer turn, so pick the max-token one. None if nothing decoded."""
        if not self.turns:
            return None
        return max(self.turns, key=lambda t: (t.get("usage") or {}).get("total_tokens") or 0)

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
        u = Usage()
        for t in self.turns:
            m = t.get("usage") or {}
            u.input_tokens += m.get("input_tokens") or 0
            u.cached_input_tokens += m.get("cached_input_tokens") or 0
            u.output_tokens += m.get("output_tokens") or 0
            u.reasoning_output_tokens += m.get("reasoning_output_tokens") or 0
            u.total_tokens += m.get("total_tokens") or 0
        p = self.primary
        if p:
            u.raw = p.get("usage") or {}
        return u

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
    """The answer: the longest decoded turn text, else the stdout transcript (fallback)."""
    texts = [t.get("text") or "" for t in turns]
    best = max(texts, key=len) if texts else ""
    return best or transcript.strip()


def _drain_stream(proc, q, timeout):
    """Drain ``codex_turn`` dicts off the result queue until codex dies (its pidfd fires) / the
    target signals done / the reader EOFs / ``timeout``. Returns the streamed turns — a live bonus
    (the JSONL is authoritative); its length is the fd-inheritance liveness probe."""
    reader = q._reader
    sentinel = getattr(proc._popen, "sentinel", None)
    watch = [reader] if sentinel is None else [reader, sentinel]
    turns, end, done = [], time.time() + timeout, False
    while not done and time.time() < end:
        ready = _conn.wait(watch, min(1.0, max(0.0, end - time.time())))
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
    return turns


def ask(prompt, *, model=None, workspace=None, timeout=300, extra_flags=None,
        codex_bin=None, extra_env=None):
    """Run one instrumented ``codex exec`` turn and return a :class:`CodexResponse`. The returned
    ``turns`` come from the authoritative capture JSONL; the live stream is drained for parity and
    probed via ``n_streamed``. Requires the built, wirecap-patched codex and codex auth
    (``OPENAI_API_KEY`` or ``codex login``)."""
    ws = ensure_git_workspace(workspace)
    capture = os.path.join(ws, "codex-capture.jsonl")
    open(capture, "w").close()   # fresh capture per run: the bridge Recorder appends + the scratch ws
    #                              is reused across calls, so start clean (also the no-stream fallback)
    q = _SPAWN.SimpleQueue()
    proc = CodexProcess(prompt, workdir=ws, capture=capture, model=model,
                        extra_flags=extra_flags, codex_bin=codex_bin, extra_env=extra_env,
                        args=(q, ("codex_turn",), timeout + 60))  # max_wait > timeout → death-based done
    proc.start()
    q._writer.close()            # parent only reads; the reader EOFs once codex (the writer holder) dies
    try:
        streamed = _drain_stream(proc, q, timeout)
    finally:
        proc.close()             # SIGTERM + blocking reap: codex is never left running, exit_status set
        for c in (q._reader, q._writer):
            try:
                c.close()
            except Exception:
                pass
    turns = _load_capture(capture) or streamed   # JSONL authoritative; the stream is the fallback
    transcript = proc.transcript
    return CodexResponse(text=_answer_text(turns, transcript), transcript=transcript,
                         turns=turns, exit_status=proc.exit_status,
                         capture_path=capture, workspace=ws, n_streamed=len(streamed))


def ask_many(prompt, n, **kwargs):
    """Run ``n`` independent one-shot turns (sequentially). Same kwargs as :func:`ask`."""
    return [ask(prompt, **kwargs) for _ in range(n)]
