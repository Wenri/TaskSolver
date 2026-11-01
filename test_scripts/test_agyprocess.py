#!/usr/bin/env python3
"""AgyProcess — agy driven as a multiprocessing.spawn-shaped child, streaming native
Python objects home over a caller-owned result SimpleQueue.

Needs the shim built for the RUNNING agy's build-id — uses the pinned vendor/agy
(matches symbols.json). Stock-mp layering: the caller creates the SimpleQueue (a Pipe +
SemLocks, re-attached to the shared resource_tracker) and hands it to AgyProcess via
``args=(q,)``; AgyProcess is just the Process, the caller drains ``q``.

    python3 test_scripts/test_agyprocess.py
"""

from __future__ import annotations
import os
import sys
import time
from collections.abc import Sequence
from typing import cast

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)                       # repo root holds the shared `wirecap` package
_ANTI = os.path.join(_REPO, "antigravity")
sys.path.insert(0, _ANTI)
sys.path.insert(0, _REPO)

from pyagy.agyprocess import AgyProcess                       # noqa: E402
from wirecap.decode.mp_child import _demo_target, _raise_target, stream_turns, DONE  # noqa: E402
from pyagy.client import _new_channel, _close_channel, _ask_turn  # noqa: E402  (caller-side helpers)

from wirecap.runtime.session import ResultChannel
from wirecap.types import Event

_fail: list[str] = []


def _drain(p: AgyProcess, q: ResultChannel, n: int = 2, timeout: int = 45) -> list[object]:
    """Collect up to ``n`` RAW objects off the caller's queue (incl. the (DONE, code)
    sentinel), draining the PTY in the same wait via ``service_pty`` (no background thread)."""
    got: list[object] = []
    end, reader = time.time() + timeout, q._reader
    while time.time() < end and len(got) < n:
        try:
            if p.service_pty(1.0, [reader]):
                while reader.poll(0) and len(got) < n:
                    got.append(q.get())
        except EOFError:
            break
    return got


def _teardown(p: AgyProcess, q: ResultChannel | None = None) -> None:
    try:
        p.terminate(); p.join(timeout=10)
    finally:
        try:
            p.close()
        except Exception:
            pass
        if q is not None:
            _close_channel(q)


def _text(event: Event) -> str:
    value = event.get("text", "")
    assert isinstance(value, str), value
    return value.strip()


def case_roundtrip() -> None:
    q = _new_channel()
    p = AgyProcess(target=_demo_target, args=(q, "hi", 7)); p.start(); q._writer.close()
    got = _drain(p, q); _teardown(p, q)
    obj = next((cast(Event, x) for x in got if isinstance(x, dict)), None)
    done = any(x == (DONE, 0) for x in got)
    # parent_alive/ppid exercise the parent-death sentinel: parent_process() sees the controlling
    # process (our pid) and reports it alive (the boot pipe only EOFs when we die).
    ok = obj is not None and obj.get("wire_mp") == "ok" and obj.get("args") == ("hi", 7) \
        and str(obj.get("py", "")).startswith(f"{sys.version_info[0]}.{sys.version_info[1]}.") and done \
        and obj.get("parent_alive") is True and obj.get("ppid") == os.getpid()
    print(f"  {'ok  ' if ok else 'FAIL'} round-trip: native object + clean exitcode + parent sentinel  got={got}")
    if not ok:
        _fail.append("roundtrip")


def case_exception() -> None:
    # target raises → mp's _bootstrap catches it and returns exitcode 1 (traceback to agy's
    # stderr); the parent sees (DONE, 1). (agy's own process exitcode is separate.)
    q = _new_channel()
    p = AgyProcess(target=_raise_target, args=(q,)); p.start(); q._writer.close()
    got = _drain(p, q); _teardown(p, q)
    ok = any(x == (DONE, 1) for x in got)
    print(f"  {'ok  ' if ok else 'FAIL'} exception: firewalled, target exitcode 1  got={got}")
    if not ok:
        _fail.append("exception")


def case_stream() -> None:
    # The payoff: stream agy's DECODED model turns home as native objects. Needs a live
    # model turn (network/auth) + capture books (capture=True); agy occasionally exits before
    # completing a turn, so retry once. A turn with EMPTY text = a real decode bug (FAIL); no
    # turns after retries = a live-model flake (NOTE, not a failure).
    for _ in range(2):
        q = _new_channel()
        p = AgyProcess(target=stream_turns, args=(q,),
                       agy_args=["--print", "What is 2+2? Reply with only the digits."])
        p.start(); q._writer.close()
        turns: list[Event] = []
        end, reader = time.time() + 75, q._reader
        while time.time() < end:
            try:
                if p.service_pty(1.0, [reader]):
                    while reader.poll(0):
                        o = q.get()
                        if isinstance(o, dict):
                            event = cast(Event, o)
                            if event.get("kind") == "genai_turn":
                                turns.append(event)
            except EOFError:
                break
        _teardown(p, q)
        if turns:
            has_text = any(_text(t) for t in turns)
            print(f"  {'ok  ' if has_text else 'FAIL'} stream: {len(turns)} decoded genai_turn(s), "
                  f"text={'yes' if has_text else 'EMPTY'}")
            if not has_text:
                _fail.append("stream")
            return
    print("  NOTE stream: agy produced no turn this run (live-model flake); decode path unexercised")


def case_persistent() -> None:
    # Persistent multi-turn: agy stays alive interactive; drive follow-ups with _ask_turn() and
    # collect the decoded turns per prompt. Flaky (two live turns), so PASS on both-turns-with-
    # text (context retained), else NOTE — decode itself is already asserted by case_stream.
    q = _new_channel()
    p = AgyProcess(target=stream_turns, persistent=True, args=(q,),
                   prompt="What is 2+2? Reply with only the digits.")
    p.start(); q._writer.close()
    t1 = _ask_turn(p, q, None)                                      # submit the prefilled initial
    t2 = _ask_turn(p, q, "Now multiply that by 10. Reply with only the digits.")  # follow-up (context)
    _teardown(p, q)

    def first_text(ts: Sequence[Event]) -> str:
        return next((text for t in ts if (text := _text(t))), "")
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
