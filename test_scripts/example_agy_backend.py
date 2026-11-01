#!/usr/bin/env python3
"""Example / smoke test: use agy as a TaskSolver-contract backend.

    pixi run python test_scripts/example_agy_backend.py

Drives the logged-in Antigravity `agy` CLI (via `agy --print` under a PTY, in a
throwaway git workspace) through the same adapter surface as TaskSolver's other
backends, and parses the answer. Requires agy to be logged in
(~/.gemini/antigravity-cli/). `pyagy` is importable in the pixi env because the
package ships it (see [tool.setuptools.packages.find]).
"""

from __future__ import annotations

import re

from pyagy import AgyModel

from tasksolver.common import ParsedAnswer, Question, TaskSpec
from tasksolver.exceptions import GPTOutputParseException


class NumberAnswer(ParsedAnswer):
    def __init__(self, value: int) -> None:
        self.value = value

    @staticmethod
    def parser(gpt_raw: str) -> NumberAnswer:
        m = re.search(r"-?\d+", gpt_raw)
        if not m:
            raise GPTOutputParseException(f"no number in {gpt_raw!r}")
        return NumberAnswer(int(m.group()))

    def __str__(self) -> str:
        return str(self.value)


def main() -> None:
    task = TaskSpec(
        name="math",
        description="Answer the math question with only a number.",
        answer_type=NumberAnswer,
        followup_func=lambda task, qs, ans, completed: Question([]),
        completed_func=lambda q, a: True,
    )
    model = AgyModel(api_key=None, task=task, model=None, timeout=180)
    parsed, raw, meta, _payload = model.run_once(
        Question(["What is 2+2? Reply with only the number."])
    )
    assert isinstance(parsed, NumberAnswer)
    print("parsed value :", parsed.value)
    print("raw content  :", repr(raw["content"]))
    print("workspace    :", meta[0]["workspace"])
    assert parsed.value == 4
    print("OK ✓")


if __name__ == "__main__":
    main()
