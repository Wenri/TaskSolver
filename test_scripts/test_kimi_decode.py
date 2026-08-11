#!/usr/bin/env python3
"""Offline tests for pykimi's kimi_turn decode (KimiTurnBuilder + the correlator wiring) and
the import-purity of the embedded-side dispatch module. No kimi bundle, no node, no network.

    python3 test_scripts/test_kimi_decode.py
"""
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_KIMI = os.path.join(_REPO, "kimi")
sys.path.insert(0, _KIMI)
sys.path.insert(0, _REPO)

from pykimi.kimi_process.kimi_decode import (  # noqa: E402
    KimiTurnBuilder, build_turn_from_events, extract_usage, summarize_request)

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


REQ = {
    "protocol": "anthropic",
    "providerType": "kimi",
    "modelName": "k3",
    "modelAlias": "kimi-code/k3",
    "thinkingEffort": "high",
    "maxTokens": 32000,
    "systemPrompt": "You are Kimi." * 10,
    "tools": [{"name": "read_file"}, {"name": "shell"}],
    "messages": [
        {"role": "system", "content": [{"type": "text", "text": "ctx"}], "toolCalls": []},
        {"role": "user", "content": [{"type": "text", "text": "first"}], "toolCalls": []},
        {"role": "user", "content": [{"type": "text", "text": "what is 2+2?"}],
         "toolCalls": []},
    ],
    "fields": {},
}

USAGE = {"inputOther": 100, "output": 40, "inputCacheRead": 900, "inputCacheCreation": 10}

FINISH = {
    "type": "finish",
    "message": {
        "role": "assistant",
        "content": [{"type": "think", "think": "let me add"},
                    {"type": "text", "text": "2+2 = "},
                    {"type": "text", "text": "4"}],
        "toolCalls": [{"type": "function", "id": "t1", "name": "read_file",
                       "arguments": "{\"path\": \"x\"}"}],
    },
    "providerFinishReason": "stop",
    "rawFinishReason": "end_turn",
}

EVENTS = [
    {"type": "part", "part": {"type": "think", "think": "let me add"}},
    {"type": "part", "part": {"type": "text", "text": "2+2 = "}},
    {"type": "part", "part": {"type": "text", "text": "4"}},
    {"type": "usage", "usage": USAGE, "model": "kimi-k3-instruct"},
    FINISH,
    {"type": "timing", "firstTokenLatencyMs": 120, "streamDurationMs": 900},
]


def test_usage_normalization():
    print("[offline] extract_usage: kosong TokenUsage -> neutral Usage names")
    u = extract_usage(EVENTS)
    check(u["input_tokens"] == 1010 and u["cached_input_tokens"] == 900
          and u["output_tokens"] == 40 and u["total_tokens"] == 1050,
          "field mapping (input = other+cacheRead+cacheCreation)")
    check(u["raw"] == USAGE, "provider dict kept under raw")
    check(extract_usage([FINISH]) is None, "no usage event -> None")


def test_request_summary():
    print("[offline] summarize_request: LLMRequestLogInput summary")
    s = summarize_request(REQ)
    check(s["model"] == "k3" and s["provider"] == "kimi" and s["protocol"] == "anthropic",
          "model/provider/protocol")
    check(s["tools"] == ["read_file", "shell"] and s["num_input"] == 3, "tools + message count")
    check(s["first_user_text"] == "what is 2+2?", "LAST user message is the prompt")


def test_turn_assembly():
    print("[offline] build_turn_from_events: finish-message authoritative")
    turn = build_turn_from_events(EVENTS, 123.0, None, (122.5, 7, REQ))
    check(turn["kind"] == "kimi_turn" and turn["text"] == "2+2 = 4", "text from finish message")
    check(turn["reasoning"] == "let me add", "think parts -> reasoning")
    check(turn["tool_calls"] == [{"id": "t1", "name": "read_file",
                                  "arguments": "{\"path\": \"x\"}"}], "tool calls")
    check(turn["model"] == "kimi-k3-instruct", "model from usage event")
    check(turn["finish_reason"] == "stop", "providerFinishReason preferred")
    check(turn["request"]["model"] == "k3" and turn["request_full"] is REQ, "request paired")
    # a stream that died before finish: the part deltas are the fallback
    partial = [e for e in EVENTS if e["type"] == "part"]
    turn = build_turn_from_events(partial, 123.0, None, None)
    check(turn["text"] == "2+2 = 4" and turn["finish_reason"] is None,
          "streamed-delta fallback without finish")
    check(turn["model"] is None and "request" not in turn, "unpaired turn stays bare")


def test_builder_terminal():
    print("[offline] KimiTurnBuilder: parse/terminal contract")
    b = KimiTurnBuilder()
    check(b.parse_events(json.dumps(EVENTS[0]).encode()) == [EVENTS[0]], "one fire = one event")
    check(b.parse_events(b"not json") == [] and b.parse_events(b"[1,2]") == [],
          "garbage/non-dict -> no events")
    check(not b.is_terminal(EVENTS[:3]) and b.is_terminal(EVENTS), "finish is the terminal")


def test_correlator_end_to_end():
    print("[offline] dispatch-shape run through BaseCorrelator")
    from wirecap.decode.capture import BaseCorrelator

    class _Sink:
        def __init__(self):
            self.turns = []

        def record(self, *a, **k):
            pass

        def event(self, obj):
            self.turns.append(obj)

    sink = _Sink()
    corr = BaseCorrelator(sink, KimiTurnBuilder())
    corr.feed_request(REQ, 100.0, stream_id=1)
    for e in EVENTS:
        corr.feed_chunk(json.dumps(e).encode(), 100.5)
    check(len(sink.turns) == 1, "one kimi_turn emitted at finish")
    t = sink.turns[0]
    check(t["text"] == "2+2 = 4" and t["request"]["model"] == "k3"
          and t["usage"]["total_tokens"] == 1050, "assembled + paired")


def test_import_purity():
    print("[purity] pykimi.kimi_process + wirecap.decode.mp_child import stdlib-only under python3 -S")
    code = ("import sys; sys.path[:0] = [%r, %r]; "
            "import pykimi.kimi_process as kp; import wirecap.decode.mp_child; "
            "assert 'tasksolver' not in sys.modules; "
            "assert 'wirecap.runtime' not in sys.modules; "
            "assert 'kimi_request' in kp._ROUTER and 'kimi_wire' in kp._ROUTER; "
            "print('pure')" % (_REPO, _KIMI))
    r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    check(r.returncode == 0 and "pure" in r.stdout,
          "purity: kimi_process + mp_child are stdlib-only (python3 -S)")
    if r.returncode != 0:
        print(r.stderr)


def main():
    test_usage_normalization()
    test_request_summary()
    test_turn_assembly()
    test_builder_terminal()
    test_correlator_end_to_end()
    test_import_purity()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
