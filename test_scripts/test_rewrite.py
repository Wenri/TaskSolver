#!/usr/bin/env python3
"""Offline rewrite-rule utility tests. Native agy request rewriting is unsupported."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)                       # repo root holds the shared `wirecap` package
_ANTIGRAVITY = os.path.join(_REPO, "antigravity")
sys.path.insert(0, _ANTIGRAVITY)
sys.path.insert(0, _REPO)
sys.path.insert(0, _HERE)

from pyagy.agy_process import rewrite as rw  # noqa: E402

from wirecap.types import Event

_failures: list[str] = []


def check(cond: object, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


class Rec:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def event(self, obj: Mapping[str, object]) -> None:
        self.events.append(dict(obj))


def test_equal_length() -> None:
    print("[offline] equal-length substitution applies")
    rec = Rec()
    reg = rw.RewriteRegistry(
        rules=[{"find": "sk-secret-000", "replace": "sk-REDACT-000"}],  # both 13 chars
        match="streamGenerateContent", recorder=rec)
    data = b'{"x":"streamGenerateContent","key":"sk-secret-000"}'
    out = reg.rewrite(1, data)
    check(out is not None and b"sk-REDACT-000" in out and b"sk-secret-000" not in out,
          "equal-length: token redacted")
    check(out is not None and len(out) == len(data), "equal-length: length preserved")
    check(any(e["kind"] == "rewrite_applied" for e in rec.events),
          "equal-length: rewrite_applied recorded")


def test_length_guard() -> None:
    print("[offline] length-changing rules are skipped by default")
    rec = Rec()
    grow = rw.RewriteRegistry(rules=[{"find": "AA", "replace": "BBBB"}],
                              match="generateContent", recorder=rec)
    data = b'{"m":"generateContent","v":"AA"}'
    check(grow.rewrite(1, data) is None, "grow: skipped (returns None)")
    check(any(e["kind"] == "rewrite_skip" and e["reason"] == "grow" for e in rec.events),
          "grow: rewrite_skip(grow) recorded")

    rec2 = Rec()
    shrink = rw.RewriteRegistry(rules=[{"find": "BBBB", "replace": "A"}],
                                match="generateContent", recorder=rec2)
    data2 = b'{"m":"generateContent","v":"BBBB"}'
    check(shrink.rewrite(1, data2) is None, "shrink: skipped by default")
    check(any(e["kind"] == "rewrite_skip" and e["reason"] == "shrink" for e in rec2.events),
          "shrink: rewrite_skip(shrink) recorded")

    allow = rw.RewriteRegistry(rules=[{"find": "BBBB", "replace": "A"}],
                               match="generateContent", allow_shrink=True)
    out = allow.rewrite(1, data2)
    check(out is not None and len(out) < len(data2), "shrink: allowed with allow_shrink")


def test_match_gate() -> None:
    print("[offline] only model-request buffers are touched")
    reg = rw.RewriteRegistry(rules=[{"find": "abc", "replace": "xyz"}],
                             match="generateContent")
    check(reg.rewrite(1, b'{"path":"/telemetry","v":"abc"}') is None,
          "gate: non-model buffer untouched")
    check(reg.rewrite(2, b'{"m":"generateContent","v":"abc"}') is not None,
          "gate: model buffer rewritten")


def test_regex() -> None:
    print("[offline] regex rule")
    reg = rw.RewriteRegistry(
        rules=[{"find": r"tok_[0-9]{3}", "replace": "tok_XXX", "regex": True}],
        match="generateContent")
    out = reg.rewrite(1, b'{"m":"generateContent","t":"tok_123"}')
    check(out is not None and b"tok_XXX" in out, "regex: pattern replaced")


def test_mtime_reload() -> None:
    print("[offline] rules file hot-reloads on mtime change")
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(path, "w") as f:
            json.dump({"match": "generateContent",
                       "rules": [{"find": "AAA", "replace": "BBB"}]}, f)
        os.environ["AGY_PROC_REWRITE_RULES"] = path
        reg = rw.RewriteRegistry.from_env()
        data = b'{"m":"generateContent","v":"AAA"}'
        check(b"BBB" in (reg.rewrite(1, data) or b""), "reload: initial rule applies")
        time.sleep(0.02)
        with open(path, "w") as f:
            json.dump({"match": "generateContent",
                       "rules": [{"find": "AAA", "replace": "CCC"}]}, f)
        os.utime(path, (time.time() + 1, time.time() + 1))  # force mtime change
        check(b"CCC" in (reg.rewrite(1, data) or b""), "reload: edited rule picked up")
    finally:
        os.environ.pop("AGY_PROC_REWRITE_RULES", None)
        os.remove(path)


def test_func_mode() -> None:
    print("[offline] module:func mode")
    # a stdlib-importable top-level callable: bytes.upper via a small shim module
    import types
    mod = types.ModuleType("_pyagy_rw_test")
    def redact(data: bytes) -> bytes:
        return data.replace(b"secret", b"XXXXXX")
    setattr(mod, "redact", redact)
    sys.modules["_pyagy_rw_test"] = mod
    os.environ["AGY_PROC_REWRITE"] = "_pyagy_rw_test:redact"
    try:
        reg = rw.RewriteRegistry.from_env()
        out = reg.rewrite(1, b'{"m":"generateContent","v":"secret"}')
        check(out is not None and b"XXXXXX" in out, "func: callable applied")
    finally:
        os.environ.pop("AGY_PROC_REWRITE", None)
        del sys.modules["_pyagy_rw_test"]


def main() -> None:
    test_equal_length()
    test_length_guard()
    test_match_gate()
    test_regex()
    test_mtime_reload()
    test_func_mode()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
