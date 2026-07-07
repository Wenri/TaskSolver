#!/usr/bin/env python3
"""Live end-to-end test for pycodex: a real ``codex exec`` turn, instrumented by the compiled-in
wirecap bridge, decoded to a ``codex_turn``.

Gated: skips cleanly (exit 0) if the built codex binary is missing (run ``pixi run build-codex``)
or codex isn't authenticated. Needs codex auth (``OPENAI_API_KEY`` or ``codex login``).

    python3 test_scripts/test_codex.py
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_CODEX = os.path.join(_REPO, "codex")
sys.path.insert(0, _CODEX)
sys.path.insert(0, _REPO)

from pycodex import ask                      # noqa: E402
from pycodex._env import CODEX_BIN           # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def skip(msg):
    print(f"NOTE: skipping — {msg}")
    print("PASS")
    sys.exit(0)


def main():
    print("[live] pycodex.ask() end-to-end")
    if not os.path.exists(CODEX_BIN):
        skip(f"codex binary missing ({CODEX_BIN}) — run `pixi run build-codex`")
    if not os.environ.get("CONDA_PREFIX"):
        skip("CONDA_PREFIX unset — run under the pixi env (PYTHONHOME for the embedded interpreter)")

    r = ask("What is 2+2? Reply with just the number.")
    if r.exit_status != 0 and not r.turns:
        skip(f"codex exited {r.exit_status} with no decoded turn (not authenticated?)\n{r.transcript[:400]}")

    check("4" in r.text, "answer contains 4")
    check(len(r.turns) >= 1, "at least one codex_turn decoded")
    check(bool(r.model), f"served model decoded ({r.model})")
    check(r.usage.total_tokens > 0, f"usage decoded (total={r.usage.total_tokens})")
    check(r.request is not None, "request summary decoded (paired)")
    # the request the model saw carries our prompt
    first_user = (r.request or {}).get("first_user_text", "")
    check("2+2" in first_user, "request first_user_text carries the prompt")

    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
