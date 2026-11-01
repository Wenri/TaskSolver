#!/usr/bin/env python3
"""Tests for the pyagy public client (client.py).

Offline (always run): AgyResponse primary-turn selection + usage summation + accessor
properties, RewriteRule serialization, unsupported rewriting rejection, linked
resolution, and the transcript answer filter.

Live (skips cleanly per test_trampoline.py): a real ask() returns decoded turns with
text/model/usage.

    python3 test_scripts/test_client.py
"""

from __future__ import annotations

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

from pyagy import client as C  # noqa: E402
from pyagy.client import AgyResponse, RewriteRule  # noqa: E402

from wirecap.types import Event

SHIM = os.path.join(_ANTIGRAVITY, "vendor", "libagy.so")
AGY = os.path.expanduser(os.environ.get("AGY_BIN", os.path.join(_ANTIGRAVITY, "vendor", "agy")))

_failures: list[str] = []


def check(cond: object, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def _resp(turns: list[Event], transcript: str="hi\n", linked: bool=True):
    return AgyResponse(text=C._answer_text(transcript), transcript=transcript,
                       turns=turns, exit_status=0, capture_path=None,
                       workspace="/tmp", linked=linked)


def test_response_accessors() -> None:
    print("[offline] AgyResponse primary/usage/accessors")
    # usage is NORMALIZED by extract_usage to the neutral names shared with pycodex; the provider's
    # own dict rides along under "raw" (Gemini keeps per-modality/tool-use fields there).
    turns: list[Event] = [
        {"kind": "genai_turn", "text": "title",
         "usage": {"total_tokens": 58, "raw": {"totalTokenCount": 58}},
         "request": {"model": "flash-lite", "tools": [], "first_user_text": "t"}},
        {"kind": "genai_turn", "text": "ZORPLE", "events": [{"a": 1}],
         "usage": {"input_tokens": 100, "output_tokens": 5, "total_tokens": 16000,
                   "raw": {"promptTokenCount": 100, "candidatesTokenCount": 5,
                           "totalTokenCount": 16000}},
         "request": {"model": "flash", "tools": ["a", "b"], "first_user_text": "say zorple"}},
    ]
    r = _resp(turns)
    assert r.primary is not None and r.request is not None
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


def test_answer_filter() -> None:
    print("[offline] transcript answer filter drops instrumentation noise")
    t = "[antigravity/py] worker ready (module=x)\r\nZORPLE\r\n[agy_process] smoke\r\n"
    check(C._answer_text(t) == "ZORPLE", "filter: keeps only the answer line")


def test_rewrite_rule() -> None:
    print("[offline] RewriteRule serialization")
    check(RewriteRule("Paris", "Tokyo").as_dict() ==
          {"find": "Paris", "replace": "Tokyo", "count": 0, "regex": False},
          "RewriteRule.as_dict: fields mapped to rewrite.py schema")
    check(RewriteRule("a", "b", count=2, regex=True).as_dict().get("regex") is True,
          "RewriteRule.as_dict: count/regex passed through")


def test_prepare_rewrite() -> None:
    print("[offline] unsupported native rewriting fails before launch")
    with tempfile.TemporaryDirectory() as directory:
        for spec in ([RewriteRule("x", "y")], "mymod:myfunc"):
            try:
                C._check_rewrite(spec)
                check(False, "rewrite rejected")
            except NotImplementedError as exc:
                check("unsupported" in str(exc), "rewrite rejected with clear error")
        check(os.listdir(directory) == [], "unsupported rewrite creates no rule file")
    try:
        C.Session(rewrite=[RewriteRule("x", "y")])
        check(False, "session rewrite rejected")
    except NotImplementedError:
        check(True, "session rewrite rejected before provisioning")
    from pyagy import _env
    for key in ("AGY_PROC_TLS_WRITE_SYNC", "AGY_PROC_REWRITE_RULES", "AGY_PROC_REWRITE"):
        try:
            _env.linked_env(base={}, extra_env={key: "1"})
            check(False, "legacy rewrite environment rejected")
        except NotImplementedError:
            check(True, "legacy rewrite environment rejected")



def test_shim_overlays() -> None:
    print("[offline] shim overlays + linked env (full book set, no stage selector)")
    d = tempfile.mkdtemp()
    env = C._shim_overlays(None, stack=True, arg_probe=True, extra_env={"K": "v"})
    check(env.get("AGY_PROC_STACK") == "1", "stack=True -> AGY_PROC_STACK=1")
    check(env.get("AGY_PROC_CGT_ARGS") == "1", "arg_probe=True -> AGY_PROC_CGT_ARGS=1")
    check(env.get("K") == "v",
          "extra_env passed through; no rewrite -> no rules file")
    env2 = C._shim_overlays(None, stack=False, arg_probe=False, extra_env=None)
    check("AGY_PROC_STACK" not in env2 and "AGY_PROC_CGT_ARGS" not in env2,
          "no overlays -> knobs absent")
    # AgyProcess always enables capture: the shim installs the full book set, gated only by
    # AGY_PROC_ENABLE (the removed AGY_PROC_STAGE selector must not reappear).
    from pyagy import _env
    ienv = _env.linked_env(capture=os.path.join(d, "c.jsonl"))
    check(ienv.get("AGY_PROC_ENABLE") == "1" and "AGY_PROC_STAGE" not in ienv,
          "linked_env: AGY_PROC_ENABLE set, no stage selector")


# synthetic funcmap for the .stacks/.call_graph decoders (link vaddr -> name)
_FUNCS = [(0x1000, "runtime.goexit"), (0x2000, "app.run"),
          (0x6000, "codeassistclient.(*CodeAssistClient).StreamGenerateContent")]


def _synthetic_capture(d: str):
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


def _diag_resp(cap: str | None, fm: str | None):
    return AgyResponse(text="x", transcript="x", turns=[], exit_status=0,
                       capture_path=cap, workspace="/tmp", linked=True, funcmap=fm)


def test_diagnostic_accessors() -> None:
    print("[offline] AgyResponse.rpc_trace / cgt_args / stacks / call_graph")
    d = tempfile.mkdtemp()
    cap, fm = _synthetic_capture(d)
    r = _diag_resp(cap, fm)

    check("StreamGenerateContent" in r.rpc_trace and "the model turn" in r.rpc_trace,
          "rpc_trace: labeled model turn")
    check(r.rpc_trace is r.rpc_trace, "rpc_trace: cached (same object)")

    check(r.cgt_args == ["arg[0] *Request { model: gemini }", "arg[1] context.Context"],
          "cgt_args: non-empty reports collected in order")

    assert r.stacks is not None
    check("2 fire(s), 1 distinct" in r.stacks, "stacks: identical stacks grouped")
    check("StreamGenerateContent" in r.stacks and "app.run" in r.stacks,
          "stacks: frames symbolized against funcmap")

    edges = r.call_graph
    assert edges is not None
    check(edges[("app.run",
                 "codeassistclient.(*CodeAssistClient).StreamGenerateContent")] == 2,
          "call_graph: caller->callee edge counted")


def test_diagnostic_graceful_empty() -> None:
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
def test_live_ask() -> None:
    print("[live] ask() end-to-end")
    if not (os.path.exists(AGY) and os.path.exists(SHIM)):
        print("NOTE: skipping — agy or shim missing")
        return
    from pyagy import ask
    r = ask("Reply with exactly the single word ZORPLE and nothing else.")
    if not r.linked:
        print(f"NOTE: skipping live asserts — not linked ({r.linked_reason})")
        return
    if "ZORPLE" not in r.text:
        print("NOTE: skipping — agy not authenticated (no answer)")
        return
    # Response side — fully decoded from the resp_chunk SSE lines (toStreamResponseChunk):
    # text, served model id, usage. These are the "full python decode" guarantees.
    check(len(r.turns) >= 1, "live: at least one genai_turn decoded")
    check(r.model and "gemini" in r.model, "live: model id decoded (served modelVersion)")
    check(r.usage.total_tokens > 0, "live: usage decoded")
    # Request capture spans every TLS write chunk; the decoder may lack a completed
    # request when capture begins mid-connection or the CLI exits during transmission.
    if r.request:
        check(r.request.get("tools") is not None, "live: request tools decoded")
    else:
        print("NOTE: no complete request was captured")


def main() -> None:
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
