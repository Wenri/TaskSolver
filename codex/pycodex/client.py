"""Public pycodex client: ``ask`` / ``ask_many`` + ``CodexResponse``.

Runs the instrumented codex (``codex exec``) as a one-shot subprocess and reads the decoded
``codex_turn``s the compiled-in wirecap bridge wrote to the capture JSONL. Mirrors
``pyagy.client``'s ``AgyResponse`` accessor shape (``.text`` / ``.primary`` / ``.model`` /
``.usage`` / ``.request``), so the two wrappers present the same surface.
"""
import json
import os
import subprocess
from dataclasses import dataclass, field

from wirecap.runtime.workspace import ensure_git_workspace

from ._env import codex_argv, instrumented_env


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


def ask(prompt, *, model=None, workspace=None, timeout=300, extra_flags=None,
        codex_bin=None, extra_env=None):
    """Run one instrumented ``codex exec`` turn and return a :class:`CodexResponse` with the
    decoded ``codex_turn``s. Requires the built, wirecap-patched codex (``pixi run build-codex``)
    and codex auth (``OPENAI_API_KEY`` or ``codex login``)."""
    ws = ensure_git_workspace(workspace)
    capture = os.path.join(ws, "codex-capture.jsonl")
    env = instrumented_env(capture, extra_env=extra_env)
    argv = codex_argv(prompt, ws, model=model, extra_flags=extra_flags, codex_bin=codex_bin)
    # stdin=DEVNULL: `codex exec` reads stdin for extra context and blocks until EOF otherwise.
    proc = subprocess.run(argv, env=env, cwd=ws, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)
    turns = _load_capture(capture)
    transcript = proc.stdout or ""
    return CodexResponse(text=_answer_text(turns, transcript), transcript=transcript,
                         turns=turns, exit_status=proc.returncode,
                         capture_path=capture, workspace=ws)


def ask_many(prompt, n, **kwargs):
    """Run ``n`` independent one-shot turns (sequentially). Same kwargs as :func:`ask`."""
    return [ask(prompt, **kwargs) for _ in range(n)]
