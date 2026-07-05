#!/usr/bin/env python3
"""Tests for the SYNC egress rewrite registry (agy_process/rewrite.py).

Offline (always run): equal-length substitution applies, length-changing rule sets
are skipped (unless allow_shrink), the model-request sniff gate, regex rules, mtime
hot-reload, and module:func mode.

Live (skips cleanly when agy is absent / unauthenticated / build-id mismatch, per
test_trampoline.py): run a real turn with AGY_PROC_TLS_WRITE_SYNC + an equal-length
redaction rule; assert agy still answers (ZORPLE), the genai_turn request shows the
redaction, a rewrite_applied event is recorded, and no crash. Negative: a growth rule
is skipped and recorded.

    python3 test_scripts/test_rewrite.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANTIGRAVITY = os.path.join(os.path.dirname(_HERE), "antigravity")
sys.path.insert(0, _ANTIGRAVITY)
sys.path.insert(0, _HERE)

from pyagy.agy_process import rewrite as rw  # noqa: E402

SHIM = os.path.join(_ANTIGRAVITY, "vendor", "antigravity.so")
AGY = os.path.expanduser(os.environ.get("AGY_BIN", "~/.local/bin/agy"))
PROMPT = "Reply with exactly the single word ZORPLE and nothing else."

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


class Rec:
    def __init__(self):
        self.events = []

    def event(self, obj):
        self.events.append(obj)


def test_equal_length():
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


def test_length_guard():
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


def test_match_gate():
    print("[offline] only model-request buffers are touched")
    reg = rw.RewriteRegistry(rules=[{"find": "abc", "replace": "xyz"}],
                             match="generateContent")
    check(reg.rewrite(1, b'{"path":"/telemetry","v":"abc"}') is None,
          "gate: non-model buffer untouched")
    check(reg.rewrite(2, b'{"m":"generateContent","v":"abc"}') is not None,
          "gate: model buffer rewritten")


def test_regex():
    print("[offline] regex rule")
    reg = rw.RewriteRegistry(
        rules=[{"find": r"tok_[0-9]{3}", "replace": "tok_XXX", "regex": True}],
        match="generateContent")
    out = reg.rewrite(1, b'{"m":"generateContent","t":"tok_123"}')
    check(out is not None and b"tok_XXX" in out, "regex: pattern replaced")


def test_mtime_reload():
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


def test_func_mode():
    print("[offline] module:func mode")
    # a stdlib-importable top-level callable: bytes.upper via a small shim module
    import types
    mod = types.ModuleType("_pyagy_rw_test")
    mod.redact = lambda data: data.replace(b"secret", b"XXXXXX")
    sys.modules["_pyagy_rw_test"] = mod
    os.environ["AGY_PROC_REWRITE"] = "_pyagy_rw_test:redact"
    try:
        reg = rw.RewriteRegistry.from_env()
        out = reg.rewrite(1, b'{"m":"generateContent","v":"secret"}')
        check(out is not None and b"XXXXXX" in out, "func: callable applied")
    finally:
        os.environ.pop("AGY_PROC_REWRITE", None)
        del sys.modules["_pyagy_rw_test"]


# --- live roundtrip ----------------------------------------------------------
def _skip(msg):
    print(f"NOTE: skipping live rewrite test — {msg}")


def test_live_roundtrip():
    print("[live] SYNC equal-length redaction on a real turn")
    if not (os.path.exists(AGY) and os.path.exists(SHIM)):
        return _skip("agy or shim missing")
    from agy_session import AgySession

    wd = tempfile.mkdtemp(prefix="agy_rw_")
    subprocess.run("git init -q && printf x>f && git add -A && "
                   "git -c user.email=t@t -c user.name=t commit -qm i",
                   shell=True, cwd=wd, check=False)

    # Baseline turn to confirm auth + capture the exact user text to target.
    cap0 = os.path.join(wd, "base.jsonl")
    log0 = os.path.join(wd, "base.log")
    s0 = AgySession(capture=cap0, log=log0, workdir=wd)
    s0.start(["--print", PROMPT])
    s0.collect(timeout=160)
    out0 = s0.transcript
    s0.close()
    logtxt = open(log0, errors="replace").read() if os.path.exists(log0) else ""
    if "build-id ok" not in logtxt:
        return _skip("shim build-id != running agy (run `make -C antigravity symbols`)")
    if "ZORPLE" not in out0:
        return _skip("agy not authenticated (baseline turn produced no answer)")

    # Equal-length redaction: replace the sentinel with a same-length placeholder.
    # 'ZORPLE' (6) -> 'ZQRPLE' (6): a framing-safe, verifiable in-request edit.
    rules = os.path.join(wd, "rules.json")
    with open(rules, "w") as f:      # default match covers streamGenerateContent
        json.dump({"rules": [{"find": "ZORPLE", "replace": "ZQRPLE"}]}, f)
    cap1 = os.path.join(wd, "rw.jsonl")
    s1 = AgySession(capture=cap1, workdir=wd, extra_env={
        "AGY_PROC_TLS_WRITE_SYNC": "1", "AGY_PROC_REWRITE_RULES": rules})
    s1.start(["--print", PROMPT])
    s1.collect(timeout=160)
    out1 = s1.transcript
    s1.close()

    applied, turns, skips = [], [], []
    for ln in open(cap1):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        k = r.get("kind")
        if k == "rewrite_applied":
            applied.append(r)
        elif k == "genai_turn":
            turns.append(r)
        elif k == "rewrite_skip":
            skips.append(r)
    check(len(applied) >= 1, "live: rewrite_applied recorded")
    # The request as actually sent shows the redaction, not the original sentinel.
    reqs = [t for t in turns if t.get("request")]
    hit = any("ZQRPLE" in (t["request"].get("first_user_text") or "") for t in reqs)
    check(hit, "live: sent request carries the redacted text (ZQRPLE)")
    crash = sum((out1 + logtxt).count(x) for x in ("throw", "unknown pc", "panic", "SIGSEGV"))
    check(crash == 0, "live: no crash under SYNC rewrite")

    # Negative: a growth rule must be skipped, and agy must still answer.
    grow = os.path.join(wd, "grow.json")
    with open(grow, "w") as f:
        json.dump({"rules": [{"find": "ZORPLE", "replace": "ZORPLE-XL"}]}, f)
    cap2 = os.path.join(wd, "grow.jsonl")
    s2 = AgySession(capture=cap2, workdir=wd, extra_env={
        "AGY_PROC_TLS_WRITE_SYNC": "1", "AGY_PROC_REWRITE_RULES": grow})
    s2.start(["--print", PROMPT])
    s2.collect(timeout=160)
    out2 = s2.transcript
    s2.close()
    grow_skips = [json.loads(l) for l in open(cap2)
                  if l.strip() and '"rewrite_skip"' in l]
    check(any(e.get("reason") == "grow" for e in grow_skips),
          "live: growth rule recorded as rewrite_skip")
    check("ZORPLE" in out2, "live: turn still completes when rewrite is skipped")


def main():
    test_equal_length()
    test_length_guard()
    test_match_gate()
    test_regex()
    test_mtime_reload()
    test_func_mode()
    test_live_roundtrip()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
