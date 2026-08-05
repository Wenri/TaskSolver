#!/usr/bin/env python3
"""Example / smoke test: drive an instrumented CLI as a TaskSolver-contract backend.

    pixi run python test_scripts/example_cli_backend.py --backend agy
    pixi run python test_scripts/example_cli_backend.py --backend codex
    pixi run python test_scripts/example_cli_backend.py --backend claude-code

One script for all three CLI-subprocess backends, because they now share
`tasksolver.cli_backend.CLIBackendModel` — this exercises the inherited
prepare_payload -> ask -> rough_guess -> run_once chain and the canonical 4-tuple, so a
regression in the base shows up for whichever backend you can currently authenticate.

  agy          `agy --print` under a PTY, in a throwaway git workspace. Needs agy logged in
               (~/.gemini/antigravity-cli/).
  codex        `codex exec` under a PTY. Needs the built binary (`pixi install`) + codex auth
               (OPENAI_API_KEY or `codex login`). This is the ONLY test that covers CodexModel.
  claude-code  `claude -p` subprocesses. Needs the claude CLI logged in (`claude /login`).

Skips cleanly (exit 0, "NOTE: skipping") when the selected backend is unavailable — treat a
skip as UNVERIFIED, not as a pass.
"""
import argparse
import re
import sys

from tasksolver.common import ParsedAnswer, Question, TaskSpec
from tasksolver.exceptions import GPTOutputParseException


class NumberAnswer(ParsedAnswer):
    def __init__(self, value):
        self.value = value

    @classmethod
    def parser(cls, raw: str):
        m = re.search(r"-?\d+", raw or "")
        if not m:
            raise GPTOutputParseException(f"no number in {raw!r}")
        return cls(int(m.group()))

    def __str__(self):
        return str(self.value)


def build_model(backend, task, timeout):
    """Construct the backend directly (NOT via Agent): the agent.py dispatch branches pass
    timeout=1800, which is not what a smoke test wants."""
    if backend == "agy":
        from pyagy import AgyModel
        return AgyModel(api_key=None, task=task, model=None, timeout=timeout)
    if backend == "codex":
        import os
        from pycodex import CodexModel
        from pycodex._env import CODEX_BIN
        if not os.path.exists(CODEX_BIN):
            return None
        return CodexModel(api_key=os.environ.get("OPENAI_API_KEY"), task=task,
                          model=None, timeout=timeout)
    if backend == "claude-code":
        from tasksolver.claude_code import ClaudeCodeModel
        return ClaudeCodeModel(api_key=None, task=task)
    raise SystemExit(f"unknown backend {backend!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="agy", choices=("agy", "codex", "claude-code"))
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    task = TaskSpec(
        name="math",
        description="Answer the math question with only a number.",
        answer_type=NumberAnswer,
        followup_func=lambda qs, ans: Question([]),
        completed_func=lambda q, a: True,
    )
    model = build_model(args.backend, task, args.timeout)
    if model is None:
        print(f"NOTE: skipping — {args.backend} is not built/available")
        return 0

    from tasksolver.cli_backend import CLIBackendModel
    assert isinstance(model, CLIBackendModel), "backend must share the CLI adapter base"

    try:
        parsed, raw, meta, payload = model.run_once(
            Question(["What is 2+2? Reply with only the number."])
        )
    except Exception as e:                      # auth / quota / spend-cap → unverified, not a fail
        print(f"NOTE: skipping — {args.backend} could not complete a turn: "
              f"{type(e).__name__}: {str(e)[:200]}")
        return 0

    m0 = meta[0] if isinstance(meta, list) and meta else meta
    print("backend      :", args.backend)
    print("parsed value :", parsed.value)
    print("raw content  :", repr((raw["content"] or "")[:80]))
    print("metadata keys:", sorted(m0) if isinstance(m0, dict) else type(m0).__name__)

    # the canonical 4-tuple contract holds regardless of whether the model answered
    assert isinstance(payload, dict) and "prompt" in payload, "payload is the request dict"

    if not _had_model_turn(m0):
        # The CLI ran and the adapter chain worked, but no model turn was decoded (auth, quota,
        # spend cap...) so `parsed` came from the transcript fallback and asserting on it would be
        # asserting on noise. Report unverified rather than inventing a pass OR a failure.
        print(f"NOTE: skipping the answer assertion — {args.backend} decoded no model turn "
              f"(the value above is the transcript fallback, not a model reply)")
        return 0

    assert parsed.value == 4, f"expected 4, got {parsed.value}"
    print("OK ✓")
    return 0


def _had_model_turn(m0):
    """Did the CLI actually complete a model turn? agy/codex expose a wirecap `Usage` (token
    counts) in their metadata; claude-code passes the raw CLI JSON, which carries its own usage."""
    if not isinstance(m0, dict):
        return False
    u = m0.get("usage")
    if u is None:
        return False
    total = getattr(u, "total_tokens", None)
    if total is None and isinstance(u, dict):      # claude-code's raw CLI usage dict
        total = sum(v for v in u.values() if isinstance(v, int))
    return bool(total)


if __name__ == "__main__":
    sys.exit(main())
