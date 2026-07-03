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
from pyagy.agy_process.mp_child import _demo_target, _raise_target  # noqa: E402

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


if __name__ == "__main__":
    print("[AgyProcess] real multiprocessing.spawn child (agy = vendor/agy)")
    case_roundtrip()
    case_exception()
    print("\nPASS" if not _fail else "\nFAIL: " + ",".join(_fail))
    sys.exit(1 if _fail else 0)
