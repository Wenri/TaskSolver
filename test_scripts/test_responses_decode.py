#!/usr/bin/env python3
"""Offline unit tests for the codex OpenAI-Responses turn decoder + correlator.

No codex binary, no network, no key — a format-accurate synthetic ``/v1/responses`` request +
a streamed ``ResponsesStreamEvent`` sequence driven through the shared BaseCorrelator (the
pre-parsed feed_request/feed_events path codex uses), asserting the assembled ``codex_turn``.

    python3 test_scripts/test_responses_decode.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import cast

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)                       # repo root holds `wirecap`
_CODEX = os.path.join(_REPO, "codex")                # holds `pycodex`
sys.path.insert(0, _CODEX)
sys.path.insert(0, _REPO)

from pycodex.codex_process import responses_decode as rd  # noqa: E402

from wirecap.decode.capture import BaseCorrelator  # noqa: E402
from wirecap.types import Event, JSONObject

_failures: list[str] = []


def check(cond: object, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


class FakeRec:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def event(self, obj: Mapping[str, object]) -> None:
        self.events.append(dict(obj))


# --- fixture: a streamGenerateContent-equivalent Responses turn -------------------
REQUEST: JSONObject = {
    "model": "gpt-5-codex",
    "instructions": "You are a helpful coding agent.",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Say ZORPLE"}]}],
    "tools": [{"type": "function", "name": "shell"}, {"type": "function", "name": "apply_patch"}],
    "stream": True,
}
# The stream: created → two text deltas → a reasoning delta → completed (usage + model).
EVENTS: list[JSONObject] = [
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


def test_builder_units() -> None:
    print("[decode] responses_decode field extractors")
    check(rd.assemble_text(EVENTS) == "ZORPLE", "text assembles across output_text.delta")
    message: JSONObject = {"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": "complete answer"}]}
    done: JSONObject = {"type": "response.output_item.done", "item": message}
    check(rd.assemble_text([done]) == "complete answer", "completed message works without deltas")
    check(rd.assemble_text([*EVENTS, done]) == "ZORPLE", "completed item does not duplicate streamed text")
    check(rd.assemble_text([{"type": "response.completed", "response": {"output": [message]}}])
          == "complete answer", "terminal response output works without item events")
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


def test_correlator_pre_parsed() -> None:
    print("[decode] BaseCorrelator pre-parsed path (feed_request + feed_events)")
    rec = FakeRec()
    corr = BaseCorrelator(rec, rd.ResponsesTurnBuilder())
    corr.feed_request(REQUEST, t=100.0)               # the codex_request emit
    for i, ev in enumerate(EVENTS):                    # one codex_event per ResponsesStreamEvent
        corr.feed_events(rd.ResponsesTurnBuilder().parse_events(json.dumps(ev).encode()), t=100.5 + i * 0.01)
    turns = [e for e in rec.events if e.get("kind") == "codex_turn"]
    check(len(turns) == 1, "exactly one codex_turn (emitted at response.completed)")
    if turns:
        turn = turns[0]
        check(turn["text"] == "ZORPLE", "turn: text")
        check(turn["model"] == "gpt-5-codex", "turn: model")
        check(turn["finish_reason"] == "end_turn", "turn: finish_reason")
        check(cast(Mapping[str, object], turn["usage"]).get("total_tokens") == 15, "turn: usage")
        check(cast(Mapping[str, object], turn["request"]).get("first_user_text") == "Say ZORPLE",
              "turn: paired request summary")


def test_native_stream_correlation() -> None:
    print("[decode] concurrent native request IDs keep replies and cancellations separate")
    rec = FakeRec()
    corr = rd.ResponsesCorrelator(rec)
    request_a: JSONObject = {**REQUEST, "model": "model-A"}
    request_b: JSONObject = {**REQUEST, "model": "model-B"}
    corr.request(41, request_a, 1.0)
    corr.request(42, request_b, 2.0)
    chunks: list[tuple[int, JSONObject]] = [
        (41, {"type": "response.output_text.delta", "delta": "answer A"}),
        (42, {"type": "response.output_text.delta", "delta": "answer B"}),
        (41, {"type": "response.completed", "response": {"id": "resp-A"}}),
        (42, {"type": "response.completed", "response": {"id": "resp-B"}}),
    ]
    for index, (stream_id, event) in enumerate(chunks):
        corr.feed_chunk(stream_id, json.dumps(event).encode(), 3.0 + index)
    corr.finish(41)
    corr.finish(42)
    check([event["text"] for event in rec.events] == ["answer A", "answer B"],
          "interleaved output is not merged and terminal cleanup emits no duplicate")
    check([cast(Mapping[str, object], event["request"])["model"] for event in rec.events]
          == ["model-A", "model-B"], "each response preserves its exact request")
    check([event["resp_stream"] for event in rec.events] == [41, 42],
          "native response stream IDs are retained")

    corr.request(43, request_a, 10.0)
    corr.feed_chunk(43, b'{"type":"response.output_text.delta","delta":"partial"}', 11.0)
    corr.finish(43)
    check(rec.events[-1]["text"] == "partial" and rec.events[-1]["finish_reason"] is None,
          "cancelled or errored transport flushes its partial reply")
    corr.request(44, request_b, 12.0)
    corr.finish(44)
    check(len(rec.events) == 3, "failed request without response emits no invented turn")
    check(not corr._requests and not corr._events, "terminal/error cleanup releases all stream state")


def test_import_purity() -> None:
    print("[purity] pycodex.codex_process + wirecap.decode.mp_child imports stdlib-only under python3 -S")
    code = ("import sys; sys.path[:0] = [%r, %r]; import pycodex.codex_process.responses_decode; "
            "import wirecap.decode.mp_child; "   # the shared embedded-child runner must stay stdlib-pure at import
            "assert 'tasksolver' not in sys.modules; print('pure')" % (_REPO, _CODEX))
    r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    check(r.returncode == 0 and "pure" in r.stdout, "purity: codex_process + mp_child are stdlib-only (python3 -S)")
    if r.returncode != 0:
        print(r.stderr)


def main() -> None:
    test_builder_units()
    test_correlator_pre_parsed()
    test_native_stream_correlation()
    test_import_purity()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
