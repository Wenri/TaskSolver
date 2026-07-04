#!/usr/bin/env python3
"""Offline test for the RPC-trace renderer (no agy).

Feeds a synthetic capture of rpc_* + context events and asserts the timeline is
time-ordered, labeled, and counted.

    python3 test_scripts/test_rpctrace.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANTIGRAVITY = os.path.join(os.path.dirname(_HERE), "antigravity")
sys.path.insert(0, _ANTIGRAVITY)

from pyagy.agy_process import rpctrace  # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def main():
    d = tempfile.mkdtemp()
    cap = os.path.join(d, "c.jsonl")
    events = [
        {"t": 100.0, "kind": "rpc_load_code_assist", "stream": 1},
        {"t": 100.5, "kind": "rpc_fetch_userinfo", "stream": 2},
        {"t": 101.2, "kind": "rpc_stream_generate", "stream": 3},   # the model turn
        {"t": 101.9, "kind": "genai_turn", "text": "ZORPLE"},        # context
        {"t": 102.4, "kind": "rpc_record_trajectory", "stream": 4},
        {"t": 102.5, "kind": "tls_read", "stream": 9},               # ignored (not rpc/context)
        {"t": 100.9, "kind": "rpc_fetch_models", "stream": 5},       # out of order on purpose
    ]
    with open(cap, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    out = rpctrace.trace(cap)
    print("[offline] RPC trace render")
    lines = [l for l in out.splitlines() if l.strip().startswith("+")]
    # 6 rpc/context events (tls_read excluded)
    check(len(lines) == 6, "trace: only rpc_*/context events shown (tls_read excluded)")
    # time-ordered: FetchAvailableModels(100.9) before StreamGenerate(101.2)
    order = [l for l in lines]
    check("StreamGenerateContent" in out and "the model turn" in out, "trace: model turn labeled")
    check(order[0].strip().startswith("+  0.000s") and "FetchLoadCodeAssistResponse" in order[0],
          "trace: t0-relative, first event first")
    check("FetchAvailableModels" in order[2] and "+  0.900s" in order[2],
          "trace: out-of-order event sorted by time (models at t=100.9 → idx 2)")
    check("· model turn decoded" in out, "trace: genai_turn folded in as context")
    check("StreamGenerateContent=1" in out and "FetchAvailableModels=1" in out, "trace: counts")

    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
