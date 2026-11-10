#!/usr/bin/env python3
"""Offline unit tests for the Claude Anthropic-Messages turn decoder + correlator.

No Claude binary, no Bun, no network, no key — synthetic ``/v1/messages`` requests and
streamed SSE / non-streaming message events driven through ``ClaudeCorrelator``, plus a
``python3 -S`` import-purity check for ``pyclaude.claude_process``.

    python3 test_scripts/test_claude_decode.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import cast

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_CLAUDE = os.path.join(_REPO, "claude")
sys.path.insert(0, _CLAUDE)
sys.path.insert(0, _REPO)

from pyclaude.claude_process import claude_decode as cd  # noqa: E402
from wirecap.types import Event, JSONObject  # noqa: E402

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


REQUEST: JSONObject = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 16000,
    "system": [
        {"type": "text", "text": "You are Claude Code."},
        {"type": "text", "text": " Help the user."},
    ],
    "tools": [{"name": "Bash"}, {"name": "Read"}],
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "<system-reminder>\nContext\n</system-reminder>"},
                {"type": "text", "text": "Compute 2+2"},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {"command": "echo 4"}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool_1", "content": "4"},
                {"type": "text", "text": "<system-reminder>\nPost-tool reminder\n</system-reminder>"},
            ],
        },
    ],
    "stream": True,
}

EVENTS: list[JSONObject] = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6-20260301",
            "content": [],
            "stop_reason": None,
            "usage": {
                "input_tokens": 20,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 30,
                "output_tokens": 0,
            },
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "2+2 is "}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "4."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "2+2 = "}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "4"}},
    {"type": "content_block_stop", "index": 1},
    {
        "type": "content_block_start",
        "index": 2,
        "content_block": {"type": "tool_use", "id": "tool_2", "name": "Read", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 2,
        "delta": {"type": "input_json_delta", "partial_json": '{"file_path":'},
    },
    {
        "type": "content_block_delta",
        "index": 2,
        "delta": {"type": "input_json_delta", "partial_json": '"/tmp/a.txt"}'},
    },
    {"type": "content_block_stop", "index": 2},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 15},
    },
    {"type": "message_stop"},
]


def test_usage_normalization() -> None:
    print("[offline] extract_usage: Anthropic usage -> neutral Usage names")
    u = cd.extract_usage(EVENTS)
    assert u is not None
    check(
        u["input_tokens"] == 150
        and u["cached_input_tokens"] == 100
        and u["output_tokens"] == 15
        and u["total_tokens"] == 165,
        "merged message_start + message_delta usage (input = uncached+cache_read+cache_creation)",
    )
    check(cd.extract_usage([{"type": "message_stop"}]) is None, "no usage -> None")


def test_request_summary() -> None:
    print("[offline] summarize_request: Anthropic /v1/messages summary")
    s = cd.summarize_request(REQUEST)
    check(s["model"] == "claude-sonnet-4-6" and s["max_tokens"] == 16000, "model + max_tokens")
    check(s["instructions_len"] == len("You are Claude Code. Help the user."), "system blocks length")
    check(s["tools"] == ["Bash", "Read"] and s["num_input"] == 3, "tools + message count")
    check(
        s["first_user_text"] == "Compute 2+2",
        "system-reminders and tool_result-only follow-ups do not overwrite user prompt",
    )


def test_turn_assembly() -> None:
    print("[offline] build_turn_from_events: streaming + non-streaming")
    turn = cd.build_turn_from_events(EVENTS, 200.0, 9, (199.0, 9, REQUEST))
    check(turn["kind"] == "claude_turn" and turn["text"] == "2+2 = 4", "streamed text")
    check(turn["reasoning"] == "2+2 is 4.", "streamed thinking -> reasoning")
    check(
        turn["tool_calls"] == [{"id": "tool_2", "name": "Read", "arguments": {"file_path": "/tmp/a.txt"}}],
        "streamed input_json_delta -> parsed tool_calls",
    )
    check(turn["model"] == "claude-sonnet-4-6-20260301", "served model from message_start")
    check(turn["finish_reason"] == "tool_use", "stop_reason from message_delta")
    check(
        cast(Mapping[str, object], turn["request"])["first_user_text"] == "Compute 2+2"
        and turn["request_full"] is REQUEST,
        "paired request attached",
    )

    non_stream: list[JSONObject] = [
        {
            "id": "msg_02",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [
                {"type": "thinking", "thinking": "quick check"},
                {"type": "text", "text": "hello world"},
                {"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "ls"}},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }
    ]
    single = cd.build_turn_from_events(non_stream, 201.0, 10, None)
    check(
        single["text"] == "hello world"
        and single["reasoning"] == "quick check"
        and single["model"] == "claude-opus-4-7"
        and single["finish_reason"] == "end_turn"
        and cast(Mapping[str, object], single["usage"])["total_tokens"] == 14,
        "non-streaming message object fallback",
    )


def test_native_stream_correlation() -> None:
    print("[offline] ClaudeCorrelator: concurrent stream IDs and partial stream cleanup")
    rec = FakeRec()
    corr = cd.ClaudeCorrelator(rec)
    req_a: JSONObject = {**REQUEST, "model": "claude-haiku-4-5"}
    req_b: JSONObject = {**REQUEST, "model": "claude-sonnet-4-6"}
    corr.request(41, req_a, 1.0)
    corr.request(42, req_b, 2.0)
    chunks: list[tuple[int, JSONObject]] = [
        (41, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "reply A"}}),
        (42, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "reply B"}}),
        (41, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
        (41, {"type": "message_stop"}),
        (42, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
        (42, {"type": "message_stop"}),
    ]
    for index, (stream_id, event) in enumerate(chunks):
        corr.feed_chunk(stream_id, json.dumps(event).encode(), 3.0 + index)
    corr.finish(41)
    corr.finish(42)
    check(
        [event["text"] for event in rec.events] == ["reply A", "reply B"],
        "interleaved streams stay separate and finish() after message_stop emits no duplicate",
    )
    check(
        [cast(Mapping[str, object], event["request"])["model"] for event in rec.events]
        == ["claude-haiku-4-5", "claude-sonnet-4-6"],
        "each turn pairs with its own request",
    )
    corr.request(43, req_a, 10.0)
    corr.feed_chunk(
        43,
        b'{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}',
        11.0,
    )
    corr.finish(43)
    check(
        rec.events[-1]["text"] == "partial" and rec.events[-1]["finish_reason"] is None,
        "aborted stream flushes partial turn on claude_end",
    )
    corr.request(44, req_b, 12.0)
    corr.finish(44)
    check(len(rec.events) == 3, "request without response events emits no empty turn")


def test_import_purity() -> None:
    print("[purity] pyclaude.claude_process + wirecap.decode.mp_child import stdlib-only under python3 -S")
    code = (
        "import sys; sys.path[:0] = [%r, %r]; "
        "import pyclaude.claude_process as cp; import wirecap.decode.mp_child; "
        "assert 'tasksolver' not in sys.modules; "
        "assert 'wirecap.runtime' not in sys.modules; "
        "assert 'claude_request' in cp._ROUTER and 'claude_event' in cp._ROUTER; "
        "print('pure')" % (_REPO, _CLAUDE)
    )
    r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    check(
        r.returncode == 0 and "pure" in r.stdout,
        "purity: claude_process + mp_child are stdlib-only (python3 -S)",
    )
    if r.returncode != 0:
        print(r.stderr)


def main() -> None:
    test_usage_normalization()
    test_request_summary()
    test_turn_assembly()
    test_native_stream_correlation()
    test_import_purity()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
