#!/usr/bin/env python3
"""Offline tests for the app-boundary RESPONSE capture (Plan 7).

No agy, no network. Covers the boundary-crossing Python logic: (1) the embedded-side
`dispatch("app_response", ...)` records the FULL answer text (not preview-truncated),
(2) the client's `_load_capture` extracts `app_response` texts alongside wire
`genai_turn`s in one pass, and (3) `AgyResponse.app_text`/`.source`/`.text` prefer the
app-boundary answer with a wire→transcript fallback. The C decode (updateWithStep
rsi+0x8 → `app_response`) is exercised by the live probe, not here.

    python3 test_scripts/test_appresponse.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANTIGRAVITY = os.path.join(os.path.dirname(_HERE), "antigravity")
sys.path.insert(0, _ANTIGRAVITY)

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_dispatch_records_full_text():
    print("[offline] dispatch('app_response') records the full untruncated answer")
    from pyagy import agy_process as ap
    captured = []
    orig = ap._rec.event
    ap._rec.event = lambda ev: captured.append(ev)   # capture instead of persist
    try:
        big = "The mitochondria is the powerhouse of the cell. " * 500  # ~24 KB
        ap.dispatch("app_response", 0xdead, big.encode())
    finally:
        ap._rec.event = orig
    check(len(captured) == 1, "one event emitted")
    check(captured and captured[0].get("kind") == "app_response", "kind is app_response")
    check(captured and captured[0].get("text") == big, "full text stored (no preview truncation)")


def _capture(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_load_capture():
    print("[offline] client._load_capture: one pass yields wire turns + app texts")
    from pyagy import client
    d = tempfile.mkdtemp()
    cap = os.path.join(d, "cap.jsonl")
    _capture(cap, [
        {"kind": "genai_turn", "usage": {"totalTokenCount": 10}},
        {"kind": "app_response", "text": "ALPHA BETA GAMMA"},
        {"kind": "app_response", "text": "ALPHA BETA GAMMA"},   # duplicate full snapshot
        {"kind": "app_response", "text": ""},                    # empty → skipped
        {"kind": "callstack", "src": "x", "frames": []},         # unrelated → ignored
    ])
    turns, app_texts, cur = client._load_capture(cap)
    check(len(turns) == 1, "genai_turn extracted")
    check(app_texts == ["ALPHA BETA GAMMA", "ALPHA BETA GAMMA"], "non-empty app_response texts extracted")
    check(cur == 5, "cursor at last line")
    # cursor slicing (multi-turn): nothing new past the end
    _, more, cur2 = client._load_capture(cap, since=cur)
    check(more == [] and cur2 == cur, "cursor resume yields no re-reads")


def test_response_preference():
    print("[offline] AgyResponse.source / app_text / text preference")
    from pyagy.client import AgyResponse
    # app present → source 'app', text prefers the longest app answer
    r = AgyResponse(text="from-transcript", transcript="from-transcript", turns=[{}],
                    exit_status=0, capture_path=None, workspace="/w", instrumented=True,
                    app_turns=["short", "the longer full answer"])
    check(r.source == "app", "source == app when app_turns present")
    check(r.app_text == "the longer full answer", "app_text is the longest snapshot")

    # no app, wire turns present → 'wire'
    r2 = AgyResponse(text="t", transcript="t", turns=[{}], exit_status=0,
                     capture_path=None, workspace="/w", instrumented=True, app_turns=[])
    check(r2.source == "wire", "source == wire when only genai_turns present")
    check(r2.app_text == "", "app_text empty without app_turns")

    # nothing decoded → 'transcript'
    r3 = AgyResponse(text="t", transcript="t", turns=[], exit_status=0,
                     capture_path=None, workspace="/w", instrumented=False, app_turns=[])
    check(r3.source == "transcript", "source == transcript with no decoded events")


def test_import_purity():
    print("[offline] agy_process (incl. app_response route) stays stdlib-only")
    code = ("import sys; sys.path.insert(0, %r); import pyagy.agy_process as ap; "
            "assert 'app_response' in ap._ROUTER; "
            "assert 'tasksolver' not in sys.modules; print('pure')" % _ANTIGRAVITY)
    import subprocess
    r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    check(r.returncode == 0 and "pure" in r.stdout, "purity: app_response routed, no tasksolver")
    if r.returncode != 0:
        print(r.stderr)


def main():
    test_dispatch_records_full_text()
    test_load_capture()
    test_response_preference()
    test_import_purity()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
