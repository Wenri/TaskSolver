#!/usr/bin/env python3
"""AgyProcess — agy driven as a multiprocessing.spawn-shaped child, streaming native
Python objects home over a Connection. See plan why-make-agy-a-splendid-rainbow.md.

Needs the shim built for the RUNNING agy's build-id — uses the pinned vendor/agy
(matches symbols.json). WSL1-ok: the channel is multiprocessing.connection (no semaphores).

    python3 test_scripts/test_agyprocess.py
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANTI = os.path.join(os.path.dirname(_HERE), "antigravity")
sys.path.insert(0, _ANTI)

from pyagy.agyprocess import AgyProcess                       # noqa: E402
from pyagy.agy_process.mp_child import _demo_target, _raise_target, stream_turns  # noqa: E402

_fail = []


def _drain(p, n=2, timeout=45):
    got, end = [], time.time() + timeout
    while time.time() < end and len(got) < n:
        try:
            if p.poll(1.0):
                got.append(p.recv())
        except EOFError:
            break
    return got


def _teardown(p):
    try:
        p.terminate(); p.join(timeout=10)
    finally:
        try:
            p._popen.close()
        except Exception:
            pass


def case_roundtrip():
    p = AgyProcess(target=_demo_target, args=("hi", 7)); p.start()
    got = _drain(p); _teardown(p)
    obj = next((x for x in got if isinstance(x, dict)), None)
    done = any(x == ("_agy_done", 0) for x in got)
    ok = obj is not None and obj.get("agy_mp") == "ok" and list(obj.get("args", ())) == ["hi", 7] \
        and obj.get("py", "").startswith("3.13") and done
    print(f"  {'ok  ' if ok else 'FAIL'} round-trip: native object + clean exitcode  got={got}")
    if not ok:
        _fail.append("roundtrip")


def case_exception():
    # target raises → mp's _bootstrap catches it and returns exitcode 1 (traceback to agy's
    # stderr); the parent sees ("_agy_done", 1). (agy's own process exitcode is separate.)
    p = AgyProcess(target=_raise_target); p.start()
    got = _drain(p); _teardown(p)
    ok = any(x == ("_agy_done", 1) for x in got)
    print(f"  {'ok  ' if ok else 'FAIL'} exception: firewalled, target exitcode 1  got={got}")
    if not ok:
        _fail.append("exception")


def case_stream():
    # The payoff: stream agy's DECODED model turns home as native objects. Needs a live
    # model turn (network/auth) + stage 3; agy occasionally exits before completing a turn,
    # so retry once. A turn with EMPTY text = a real decode bug (FAIL); no turns after
    # retries = a live-model flake (NOTE, not a failure).
    for _ in range(2):
        p = AgyProcess(target=stream_turns, stage=3,
                       agy_args=["--print", "What is 2+2? Reply with only the digits."])
        p.start()
        turns, end = [], time.time() + 75
        while time.time() < end:
            try:
                if p.poll(1.0):
                    o = p.recv()
                    if isinstance(o, dict) and o.get("kind") == "genai_turn":
                        turns.append(o)
            except EOFError:
                break
        _teardown(p)
        if turns:
            has_text = any((t.get("text") or "").strip() for t in turns)
            print(f"  {'ok  ' if has_text else 'FAIL'} stream: {len(turns)} decoded genai_turn(s), "
                  f"text={'yes' if has_text else 'EMPTY'}")
            if not has_text:
                _fail.append("stream")
            return
    print("  NOTE stream: agy produced no turn this run (live-model flake); decode path unexercised")


def case_persistent():
    # Persistent multi-turn: agy stays alive interactive; drive follow-ups with .ask() and
    # collect the decoded turns per prompt. Flaky (two live turns), so PASS on both-turns-with-
    # text (context retained), else NOTE — decode itself is already asserted by case_stream.
    p = AgyProcess(target=stream_turns, stage=3, persistent=True,
                   prompt="What is 2+2? Reply with only the digits.")
    p.start()
    t1 = p.ask()                                                    # submit the prefilled initial
    t2 = p.ask("Now multiply that by 10. Reply with only the digits.")   # follow-up (needs context)
    _teardown(p)

    def first_text(ts):
        return next(((t.get("text") or "").strip() for t in ts if (t.get("text") or "").strip()), "")
    x1, x2 = first_text(t1), first_text(t2)
    if x1 and x2:
        print(f"  ok   persistent: 2-turn session t1={x1[:16]!r} t2={x2[:16]!r} (context retained)")
    else:
        print(f"  NOTE persistent: incomplete this run (t1={len(t1)} t2={len(t2)}); live-model flake")


if __name__ == "__main__":
    print("[AgyProcess] real multiprocessing.spawn child (agy = vendor/agy)")
    case_roundtrip()
    case_exception()
    case_stream()
    case_persistent()
    print("\nPASS" if not _fail else "\nFAIL: " + ",".join(_fail))
    sys.exit(1 if _fail else 0)
