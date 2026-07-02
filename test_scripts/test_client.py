#!/usr/bin/env python3
"""Tests for the pyagy public client (client.py).

Offline (always run): AgyResponse primary-turn selection + usage summation + accessor
properties, RewriteRule serialization, rewrite-spec preparation (rules file + str +
<locals> rejection), instrumented resolution, and the transcript answer filter.

Live (skips cleanly per test_trampoline.py): a real ask() returns decoded turns with
text/model/usage, and a RewriteRule redaction is reflected on the wire.

    python3 test_scripts/test_client.py
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANTIGRAVITY = os.path.join(os.path.dirname(_HERE), "antigravity")
sys.path.insert(0, _ANTIGRAVITY)

from pyagy import client as C  # noqa: E402
from pyagy.client import AgyResponse, RewriteRule, ToolSpec  # noqa: E402

SHIM = os.path.join(_ANTIGRAVITY, "vendor", "antigravity.so")
AGY = os.path.expanduser(os.environ.get("AGY_BIN", "~/.local/bin/agy"))

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def _resp(turns, transcript="hi\n", instrumented=True):
    return AgyResponse(text=C._answer_text(transcript), transcript=transcript,
                       turns=turns, exit_status=0, capture_path=None,
                       workspace="/tmp", instrumented=instrumented)


def test_response_accessors():
    print("[offline] AgyResponse primary/usage/accessors")
    turns = [
        {"kind": "genai_turn", "text": "title", "usage": {"totalTokenCount": 58},
         "request": {"model": "flash-lite", "tools": [], "first_user_text": "t"}},
        {"kind": "genai_turn", "text": "ZORPLE", "events": [{"a": 1}],
         "usage": {"promptTokenCount": 100, "candidatesTokenCount": 5, "totalTokenCount": 16000},
         "request": {"model": "flash", "tools": ["a", "b"], "first_user_text": "say zorple"}},
    ]
    r = _resp(turns)
    check(r.primary["text"] == "ZORPLE", "primary: picks the max-token (agent) turn")
    check(r.model == "flash", "model: from primary request")
    check(r.request["tools"] == ["a", "b"], "request: from primary")
    check(r.events == [{"a": 1}], "events: from primary")
    check(r.usage.total_tokens == 16058, "usage: summed across turns")
    check(r.usage.prompt_tokens == 100, "usage: prompt tokens from primary")
    check(str(r) == "hi", "__str__: returns .text")

    empty = _resp([])
    check(empty.primary is None and empty.request is None and empty.usage.total_tokens == 0,
          "empty: no turns → safe defaults")


def test_answer_filter():
    print("[offline] transcript answer filter drops instrumentation noise")
    t = "[antigravity/py] worker ready (module=x)\r\nZORPLE\r\n[agy_process] smoke\r\n"
    check(C._answer_text(t) == "ZORPLE", "filter: keeps only the answer line")


def test_rewrite_rule():
    print("[offline] RewriteRule serialization")
    check(RewriteRule("Paris", "Tokyo").as_dict() ==
          {"find": "Paris", "replace": "Tokyo", "count": 0, "regex": False},
          "RewriteRule.as_dict: fields mapped to rewrite.py schema")
    check(RewriteRule("a", "b", count=2, regex=True).as_dict()["regex"] is True,
          "RewriteRule.as_dict: count/regex passed through")


def test_prepare_rewrite():
    print("[offline] rewrite-spec preparation")
    import json
    d = tempfile.mkdtemp()
    env, path = C._prepare_rewrite([RewriteRule("x", "y")], d)
    check(env.get("AGY_PROC_TLS_WRITE_SYNC") == "1", "rules: enables SYNC")
    check(env.get("AGY_PROC_REWRITE_RULES") == path and os.path.exists(path),
          "rules: rules file written + wired")
    check(json.load(open(path))["rules"][0]["find"] == "x", "rules: content serialized")

    env2, p2 = C._prepare_rewrite("mymod:myfunc", d)
    check(env2.get("AGY_PROC_REWRITE") == "mymod:myfunc" and p2 is None,
          "str spec: module:func wired, no rules file")

    try:
        C._prepare_rewrite(lambda data: data, d)   # <locals> callable
        check(False, "locals: rejected")
    except ValueError:
        check(True, "locals: callable rejected with clear error")


def test_resolve_instrumented():
    print("[offline] instrumented resolution")
    check(C._resolve_instrumented(False) == (False, "instrumented=False"),
          "resolve: explicit False")
    use, reason = C._resolve_instrumented(None)
    if os.path.exists(SHIM):
        check(use is True, "resolve: None + shim present → instrumented")
    else:
        check(use is False and reason, "resolve: None + no shim → fallback with reason")


# --- live --------------------------------------------------------------------
def test_live_ask():
    print("[live] ask() end-to-end")
    if not (os.path.exists(AGY) and os.path.exists(SHIM)):
        print("NOTE: skipping — agy or shim missing")
        return
    from pyagy import ask
    r = ask("Reply with exactly the single word ZORPLE and nothing else.")
    if not r.instrumented:
        print(f"NOTE: skipping live asserts — not instrumented ({r.instrumented_reason})")
        return
    if "ZORPLE" not in r.text:
        print("NOTE: skipping — agy not authenticated (no answer)")
        return
    check(len(r.turns) >= 1, "live: at least one genai_turn decoded")
    check(r.model and "gemini" in r.model, "live: model id decoded")
    check(r.usage.total_tokens > 0, "live: usage decoded")
    check((r.request or {}).get("tools"), "live: request tools decoded")

    r2 = ask("Reply with exactly the single word ZORPLE and nothing else.",
             rewrite=[RewriteRule("ZORPLE", "ZQRPLE")])
    sent = (r2.request or {}).get("first_user_text") or ""
    check("ZQRPLE" in sent, "live: rewrite reflected in the sent request")
    check("ZQRPLE" in r2.text, "live: agy answered with the rewritten word")


def main():
    test_response_accessors()
    test_answer_filter()
    test_rewrite_rule()
    test_prepare_rewrite()
    test_resolve_instrumented()
    test_live_ask()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
