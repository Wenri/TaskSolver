#!/usr/bin/env python3
"""Example / smoke test: use agy as a TaskSolver-contract backend.

    pixi run python agyhook/python/example_agy_backend.py

Drives the logged-in Antigravity `agy` CLI (via `agy --print` under a PTY, in a
throwaway git workspace) through the same adapter surface as TaskSolver's other
backends, and parses the answer. Requires agy to be logged in
(~/.gemini/antigravity-cli/). `agy` is importable in the pixi env because
[tool.pixi.activation.env] puts agyhook/python on PYTHONPATH.
"""
import re

from tasksolver.common import ParsedAnswer, Question, TaskSpec
from tasksolver.exceptions import GPTOutputParseException

from agy import AgyModel


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


def main():
    task = TaskSpec(
        name="math",
        description="Answer the math question with only a number.",
        answer_type=NumberAnswer,
        followup_func=lambda qs, ans: Question([]),
        completed_func=lambda q, a: True,
    )
    model = AgyModel(api_key=None, task=task, model=None, print_timeout=180)
    parsed, raw, meta, payload = model.run_once(
        Question(["What is 2+2? Reply with only the number."])
    )
    print("parsed value :", parsed.value)
    print("raw content  :", repr(raw["content"]))
    print("workspace    :", meta[0]["workspace"] if isinstance(meta, list) else meta)
    assert parsed.value == 4
    print("OK ✓")


if __name__ == "__main__":
    main()
