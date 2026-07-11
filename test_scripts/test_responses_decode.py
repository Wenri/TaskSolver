#!/usr/bin/env python3
"""Offline unit tests for the codex OpenAI-Responses turn decoder + correlator.

No codex binary, no network, no key — a format-accurate synthetic ``/v1/responses`` request +
a streamed ``ResponsesStreamEvent`` sequence driven through the shared BaseCorrelator (the
pre-parsed feed_request/feed_events path codex uses), asserting the assembled ``codex_turn``.

    python3 test_scripts/test_responses_decode.py
"""
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)                       # repo root holds `wirecap`
_CODEX = os.path.join(_REPO, "codex")                # holds `pycodex`
sys.path.insert(0, _CODEX)
sys.path.insert(0, _REPO)

from pycodex.codex_process import responses_decode as rd   # noqa: E402
from wirecap.decode.capture import BaseCorrelator           # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


class FakeRec:
    def __init__(self):
        self.events = []

    def event(self, obj):
        self.events.append(obj)


# --- fixture: a streamGenerateContent-equivalent Responses turn -------------------
REQUEST = {
    "model": "gpt-5-codex",
    "instructions": "You are a helpful coding agent.",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Say ZORPLE"}]}],
    "tools": [{"type": "function", "name": "shell"}, {"type": "function", "name": "apply_patch"}],
    "stream": True,
}
# The stream: created → two text deltas → a reasoning delta → completed (usage + model).
EVENTS = [
    {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5-codex"}},
    {"type": "response.output_text.delta", "delta": "ZOR"},
    {"type": "response.reasoning_text.delta", "delta": "thinking...", "content_index": 0},
    {"type": "response.output_text.delta", "delta": "PLE"},
    {"type": "response.completed", "response": {
        "id": "resp_1", "model": "gpt-5-codex", "end_turn": True,
        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
                  "input_tokens_details": {"cached_tokens": 4},
                  "output_tokens_details": {"reasoning_tokens": 1}}}},
]


def test_builder_units():
    print("[decode] responses_decode field extractors")
    check(rd.assemble_text(EVENTS) == "ZORPLE", "text assembles across output_text.delta")
    check(rd.assemble_reasoning(EVENTS) == "thinking...", "reasoning assembles")
    check(rd.model_version(EVENTS) == "gpt-5-codex", "model from completed response")
    check(rd.finish_reason(EVENTS) == "end_turn", "finish = end_turn")
    u = rd.extract_usage(EVENTS)
    check(u.get("total_tokens") == 15 and u.get("cached_input_tokens") == 4
          and u.get("reasoning_output_tokens") == 1, "usage normalized (total/cached/reasoning)")
    s = rd.summarize_request(REQUEST)
    check(s["model"] == "gpt-5-codex", "request: model")
    check(s["tools"] == ["shell", "apply_patch"], "request: tool names")
    check(s["first_user_text"] == "Say ZORPLE", "request: first user text")


def test_correlator_pre_parsed():
    print("[decode] BaseCorrelator pre-parsed path (feed_request + feed_events)")
    rec = FakeRec()
    corr = BaseCorrelator(rec, rd.ResponsesTurnBuilder())
    corr.feed_request(REQUEST, t=100.0)               # the codex_request emit
    for i, ev in enumerate(EVENTS):                    # one codex_event per ResponsesStreamEvent
        corr.feed_events(rd.ResponsesTurnBuilder().parse_events(json.dumps(ev)), t=100.5 + i * 0.01)
    turns = [e for e in rec.events if e.get("kind") == "codex_turn"]
    check(len(turns) == 1, "exactly one codex_turn (emitted at response.completed)")
    if turns:
        turn = turns[0]
        check(turn["text"] == "ZORPLE", "turn: text")
        check(turn["model"] == "gpt-5-codex", "turn: model")
        check(turn["finish_reason"] == "end_turn", "turn: finish_reason")
        check(turn["usage"].get("total_tokens") == 15, "turn: usage")
        check((turn.get("request") or {}).get("first_user_text") == "Say ZORPLE",
              "turn: paired request summary")


def test_import_purity():
    print("[purity] pycodex.codex_process + wirecap.decode.mp_child imports stdlib-only under python3 -S")
    code = ("import sys; sys.path[:0] = [%r, %r]; import pycodex.codex_process.responses_decode; "
            "import wirecap.decode.mp_child; "   # the shared embedded-child runner must stay stdlib-pure at import
            "assert 'tasksolver' not in sys.modules; print('pure')" % (_REPO, _CODEX))
    r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    check(r.returncode == 0 and "pure" in r.stdout, "purity: codex_process + mp_child are stdlib-only (python3 -S)")
    if r.returncode != 0:
        print(r.stderr)


def main():
    test_builder_units()
    test_correlator_pre_parsed()
    test_import_purity()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
