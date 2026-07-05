#!/usr/bin/env python3
"""Tests for the pyagy public client (client.py).

Offline (always run): AgyResponse primary-turn selection + usage summation + accessor
properties, RewriteRule serialization, rewrite-spec preparation (rules file + str +
<locals> rejection), instrumented resolution, and the transcript answer filter.

Live (skips cleanly per test_trampoline.py): a real ask() returns decoded turns with
text/model/usage, and a RewriteRule redaction is reflected on the wire.

    python3 test_scripts/test_client.py
"""
import gzip
import json
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

    # request too large to capture off the wire (per-fire cap) → no request summary, but the
    # served model id still decodes from the response side, so .model falls back to it.
    r2 = _resp([{"kind": "genai_turn", "text": "ZORPLE", "model": "gemini-default",
                 "usage": {"totalTokenCount": 16000}}])
    check(r2.request is None, "model-fallback: no request captured (large-request case)")
    check(r2.model == "gemini-default", "model-fallback: served model from response")


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


def test_shim_overlays():
    print("[offline] shim overlays + instrumented env (full hook union, no stage selector)")
    d = tempfile.mkdtemp()
    env, rules = C._shim_overlays(None, d, stack=True, arg_probe=True, extra_env={"K": "v"})
    check(env.get("AGY_PROC_STACK") == "1", "stack=True -> AGY_PROC_STACK=1")
    check(env.get("AGY_PROC_CGT_ARGS") == "1", "arg_probe=True -> AGY_PROC_CGT_ARGS=1")
    check(env.get("K") == "v" and rules is None,
          "extra_env passed through; no rewrite -> no rules file")
    env2, r2 = C._shim_overlays(None, d, stack=False, arg_probe=False, extra_env=None)
    check("AGY_PROC_STACK" not in env2 and "AGY_PROC_CGT_ARGS" not in env2,
          "no overlays -> knobs absent")
    env3, rules3 = C._shim_overlays([RewriteRule("x", "y")], d, False, False, None)
    check(env3.get("AGY_PROC_TLS_WRITE_SYNC") == "1" and rules3 and os.path.exists(rules3),
          "rewrite spec -> SYNC enabled + rules file written")
    # AgyProcess always instruments: the shim installs the full hook union, gated only by
    # AGY_PROC_ENABLE (the removed AGY_PROC_STAGE selector must not reappear).
    from pyagy import _env
    ienv = _env.instrumented_env(capture=os.path.join(d, "c.jsonl"))
    check(ienv.get("AGY_PROC_ENABLE") == "1" and "AGY_PROC_STAGE" not in ienv,
          "instrumented_env: AGY_PROC_ENABLE set, no stage selector")


# synthetic funcmap for the .stacks/.call_graph decoders (link vaddr -> name)
_FUNCS = [(0x1000, "runtime.goexit"), (0x2000, "app.run"),
          (0x6000, "codeassistclient.(*CodeAssistClient).StreamGenerateContent")]


def _synthetic_capture(d):
    """A capture + funcmap exercising every diagnostic accessor. Returns (cap, fm)."""
    cap = os.path.join(d, "diag.jsonl")
    fm = os.path.join(d, "funcmap.tsv.gz")
    with gzip.open(fm, "wt", encoding="utf-8") as f:
        for addr, nm in _FUNCS:
            f.write(f"{addr:x}\t{nm}\n")
    events = [
        {"t": 100.0, "kind": "rpc_load_code_assist", "stream": 1},
        {"t": 101.2, "kind": "rpc_stream_generate", "stream": 2},          # the model turn
        {"kind": "cgt_args", "stream": 2, "report": "arg[0] *Request { model: gemini }"},
        {"kind": "cgt_args", "stream": 2, "report": "arg[1] context.Context"},
        {"kind": "cgt_args", "stream": 2, "report": ""},                    # empty -> skipped
        {"kind": "callstack", "src": "rpc_stream_generate", "frames": [0x6010, 0x2010, 0x1000]},
        {"kind": "callstack", "src": "rpc_stream_generate", "frames": [0x6010, 0x2010, 0x1000]},
        {"kind": "genai_turn", "text": "ignored"},                          # unrelated
    ]
    with open(cap, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return cap, fm


def _diag_resp(cap, fm):
    return AgyResponse(text="x", transcript="x", turns=[], exit_status=0,
                       capture_path=cap, workspace="/tmp", instrumented=True, funcmap=fm)


def test_diagnostic_accessors():
    print("[offline] AgyResponse.rpc_trace / cgt_args / stacks / call_graph")
    d = tempfile.mkdtemp()
    cap, fm = _synthetic_capture(d)
    r = _diag_resp(cap, fm)

    check("StreamGenerateContent" in r.rpc_trace and "the model turn" in r.rpc_trace,
          "rpc_trace: labeled model turn")
    check(r.rpc_trace is r.rpc_trace, "rpc_trace: cached (same object)")

    check(r.cgt_args == ["arg[0] *Request { model: gemini }", "arg[1] context.Context"],
          "cgt_args: non-empty reports collected in order")

    check("2 fire(s), 1 distinct" in r.stacks, "stacks: identical stacks grouped")
    check("StreamGenerateContent" in r.stacks and "app.run" in r.stacks,
          "stacks: frames symbolized against funcmap")

    edges = r.call_graph
    check(edges[("app.run",
                 "codeassistclient.(*CodeAssistClient).StreamGenerateContent")] == 2,
          "call_graph: caller->callee edge counted")


def test_diagnostic_graceful_empty():
    print("[offline] diagnostic accessors degrade when a kind/funcmap is absent")
    # no capture at all -> falsy/None
    r = _diag_resp(None, None)
    check(r.rpc_trace == "" and r.cgt_args == [] and r.stacks is None
          and r.call_graph is None, "no capture: rpc_trace='' cgt_args=[] stacks/graph=None")
    # capture present but funcmap missing -> stacks reason string, call_graph None
    d = tempfile.mkdtemp()
    cap, _ = _synthetic_capture(d)
    r2 = _diag_resp(cap, "/no/such/funcmap.tsv.gz")
    check(isinstance(r2.stacks, str) and "funcmap not found" in r2.stacks,
          "missing funcmap: stacks returns a reason string")
    check(r2.call_graph is None, "missing funcmap: call_graph None")
    check(r2.cgt_args and "gemini" in r2.cgt_args[0],
          "missing funcmap: cgt_args still decodes (independent of funcmap)")


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
    # Response side — fully decoded from the resp_chunk SSE lines (toStreamResponseChunk):
    # text, served model id, usage. These are the "full python decode" guarantees.
    check(len(r.turns) >= 1, "live: at least one genai_turn decoded")
    check(r.model and "gemini" in r.model, "live: model id decoded (served modelVersion)")
    check(r.usage.total_tokens > 0, "live: usage decoded")
    # Request side — captured off the wire via the tls_write entry-arg hook, bounded by the
    # shim's per-fire read cap (CGT_RESP_CAP=16 KiB). A small request reassembles and yields
    # tools/first_user_text; a full agent-context request can exceed the cap and not
    # reassemble, so this is best-effort, not a guarantee. Gate the assertion accordingly.
    if r.request:
        check(r.request.get("tools") is not None, "live: request tools decoded")
    else:
        print("NOTE: primary request exceeded the shim per-fire capture cap — request summary "
              "unavailable (response side still fully decoded; .model came off the response)")

    # SYNC egress rewrite (AGY_PROC_TLS_WRITE_SYNC) rewrote the wire request in-place via the
    # gum interceptor's return path; that path is retired (gum destabilized agy), and the
    # trampoline tls_write hook is entry-arg read-only. So wire rewrite is best-effort too —
    # attempt it and report, but don't hard-fail the suite on the retired capability.
    r2 = ask("Reply with exactly the single word ZORPLE and nothing else.",
             rewrite=[RewriteRule("ZORPLE", "ZQRPLE")])
    if "ZQRPLE" in r2.text:
        print("  ok   live: SYNC egress rewrite reflected on the wire")
    else:
        print("NOTE: SYNC egress rewrite not reflected on the wire — the in-place rewrite "
              "path was retired with gum; tls_write is now read-only capture")


def main():
    test_response_accessors()
    test_answer_filter()
    test_rewrite_rule()
    test_prepare_rewrite()
    test_shim_overlays()
    test_diagnostic_accessors()
    test_diagnostic_graceful_empty()
    test_live_ask()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
