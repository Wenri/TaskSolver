#!/usr/bin/env python3
"""Offline tests for the SHARED decode layer, imported directly as `wirecap.decode`.

    python3 test_scripts/test_wirecap_decode.py

The shared framing/usage code was previously only ever asserted THROUGH pyagy
(test_http1sse.py imports pyagy.agy_process.http1sse) and never through pycodex — so a
regression in wirecap that agy happened to tolerate could reach codex unnoticed. These
tests touch `wirecap.decode` with no provider package involved.

Stdlib-only, and it asserts that too: wirecap.decode must import under `python3 -S` with
no third-party module and no tasksolver, because the CLI's embedded interpreter imports it.
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)

from wirecap.decode import http1sse as h        # noqa: E402
from wirecap.decode.turns import Usage, primary_turn, sum_usage   # noqa: E402

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def test_inflate():
    print("[wirecap] inflate: gzip / deflate / passthrough")
    import gzip
    import zlib
    body = b"hello shared decode layer" * 8
    check(h.inflate(gzip.compress(body), "gzip") == body, "inflate: gzip")
    check(h.inflate(zlib.compress(body), "deflate") == body, "inflate: deflate")
    check(h.inflate(body, "") == body, "inflate: no encoding is a passthrough")
    check(h.inflate(b"not-really-gzip", "gzip") == b"not-really-gzip",
          "inflate: undecodable body returned unchanged (never raises)")


def test_classify():
    print("[wirecap] classify: http1 vs h2, per direction")
    check(h.classify("c2s", b"POST /v1/x HTTP/1.1\r\n") == "http1", "classify: c2s request → http1")
    check(h.classify("c2s", b"PRI * HTTP/2.0\r\n\r\n") == "h2", "classify: c2s preface → h2")
    check(h.classify("s2c", b"HTTP/1.1 200 OK\r\n") == "http1", "classify: s2c status → http1")
    check(h.classify("c2s", b"PO") is None, "classify: too-few bytes stays undecided")


def test_usage_shared():
    print("[wirecap] Usage / primary_turn / sum_usage (shared by both providers)")
    turns = [
        {"kind": "t", "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}},
        {"kind": "t", "usage": {"input_tokens": 100, "output_tokens": 5, "total_tokens": 900,
                                "cached_input_tokens": 7, "reasoning_output_tokens": 3,
                                "raw": {"totalTokenCount": 900}}},
    ]
    p = primary_turn(turns)
    check(p is turns[1], "primary_turn: picks the max-total-token turn")
    u = sum_usage(turns)
    check(u.total_tokens == 911, "sum_usage: totals across turns")
    check(u.input_tokens == 110 and u.output_tokens == 6, "sum_usage: input/output summed")
    check(u.cached_input_tokens == 7 and u.reasoning_output_tokens == 3,
          "sum_usage: cached/reasoning summed")
    check(u.raw == {"totalTokenCount": 900}, "sum_usage: carries the primary's provider-raw dict")
    # the deprecated pyagy aliases must keep resolving
    check(u.prompt_tokens == u.input_tokens and u.candidates_tokens == u.output_tokens,
          "Usage: legacy prompt_tokens/candidates_tokens aliases")
    check(primary_turn([]) is None and sum_usage([]).total_tokens == 0,
          "empty: safe defaults")
    check(isinstance(Usage(), Usage), "Usage: constructible with no args")


def test_purity():
    print("[wirecap] purity: wirecap.decode imports stdlib-only under python3 -S")
    code = ("import sys; sys.path.insert(0, %r); "
            "import wirecap.decode.http1sse, wirecap.decode.turns, wirecap.decode.capture, "
            "wirecap.decode.record, wirecap.decode.mp_child, wirecap.decode.h2reassemble; "
            "assert 'tasksolver' not in sys.modules; "
            "assert 'wirecap.runtime' not in sys.modules; print('pure')" % _REPO)
    r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    check(r.returncode == 0 and "pure" in r.stdout,
          f"purity: wirecap.decode is stdlib-only and runtime-free ({r.stderr.strip()[:120]})")


if __name__ == "__main__":
    test_inflate()
    test_classify()
    test_usage_shared()
    test_purity()
    print("\n" + (f"FAIL ({len(_fail)}): " + ", ".join(_fail) if _fail else "PASS"))
    sys.exit(1 if _fail else 0)
