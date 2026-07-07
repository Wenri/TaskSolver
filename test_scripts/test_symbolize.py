#!/usr/bin/env python3
"""Offline tests for the call-stack symbolizer (pyagy/agy_process/symbolize.py).

No agy, no network. Builds a synthetic funcmap.tsv.gz + a synthetic capture of
`callstack` events and asserts: PC→enclosing-function bisect (incl. mid-function
addresses), per-source stack rendering, and caller→callee call-graph aggregation.

    python3 test_scripts/test_symbolize.py
"""
import gzip
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)                       # repo root holds the shared `wirecap` package
_ANTIGRAVITY = os.path.join(_REPO, "antigravity")
sys.path.insert(0, _ANTIGRAVITY)
sys.path.insert(0, _REPO)

from pyagy.agy_process import symbolize as S  # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


# synthetic funcmap: (link vaddr → name), sorted
FUNCS = [
    (0x1000, "runtime.goexit"),
    (0x2000, "app.readLoop"),
    (0x3000, "app.frame"),
    (0x4000, "crypto/tls.(*Conn).Read"),
    (0x5000, "app.writeLoop"),
    (0x6000, "crypto/tls.(*Conn).Write"),
]


def _write_funcmap(path):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for addr, nm in FUNCS:
            f.write(f"{addr:x}\t{nm}\n")


def test_bisect():
    print("[offline] PC → enclosing function (bisect)")
    d = tempfile.mkdtemp()
    fm = os.path.join(d, "funcmap.tsv.gz")
    _write_funcmap(fm)
    sym = S.Symbolizer(fm)
    check(sym.name(0x4000) == "crypto/tls.(*Conn).Read", "exact entry resolves")
    check(sym.name(0x4123) == "crypto/tls.(*Conn).Read", "mid-function PC resolves to enclosing")
    check(sym.name(0x6fff) == "crypto/tls.(*Conn).Write", "last function covers up to next entry")
    check(sym.name(0x10) == sym.name(0x10), "below-range PC doesn't crash")
    return sym


def _capture(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_render_and_graph(sym):
    print("[offline] stack rendering + call-graph aggregation")
    d = tempfile.mkdtemp()
    cap = os.path.join(d, "cap.jsonl")
    # tls_read fired twice with the SAME stack (should group), tls_write once.
    read_frames = [0x4123, 0x3010, 0x2010, 0x1000]     # Read <- frame <- readLoop <- goexit
    write_frames = [0x6010, 0x5010, 0x1000]            # Write <- writeLoop <- goexit
    _capture(cap, [
        {"kind": "callstack", "src": "tls_read", "frames": read_frames},
        {"kind": "callstack", "src": "tls_read", "frames": read_frames},
        {"kind": "callstack", "src": "tls_write", "frames": write_frames},
        {"kind": "genai_turn", "text": "ignored"},     # non-callstack ignored
    ])
    rendered = S.render_stacks(cap, sym)
    check("tls_read: 2 fire(s), 1 distinct" in rendered, "render: groups identical stacks")
    check("crypto/tls.(*Conn).Read" in rendered and "app.readLoop" in rendered,
          "render: frames symbolized")

    edges = S.call_graph(cap, sym)
    # chain = [src, frame0, frame1, ...]; edge = (caller=deeper, callee=shallower)
    check(edges[("app.frame", "crypto/tls.(*Conn).Read")] == 2, "graph: read edge counted x2")
    check(edges[("app.readLoop", "app.frame")] == 2, "graph: caller->callee up the chain")
    check(edges[("app.writeLoop", "crypto/tls.(*Conn).Write")] == 1, "graph: write edge")
    check(("app.readLoop", "app.frame") in edges, "graph: distinct edges present")


def test_import_purity():
    print("[offline] symbolize is NOT auto-loaded by the embedded agy_process")
    code = ("import sys; sys.path[:0] = [%r, %r]; import pyagy.agy_process; "
            "assert 'pyagy.agy_process.symbolize' not in sys.modules; "
            "assert 'tasksolver' not in sys.modules; print('pure')" % (_REPO, _ANTIGRAVITY))
    import subprocess
    r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    check(r.returncode == 0 and "pure" in r.stdout, "purity: agy_process stays stdlib, no symbolize")
    if r.returncode != 0:
        print(r.stderr)


def main():
    sym = test_bisect()
    test_render_and_graph(sym)
    test_import_purity()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
