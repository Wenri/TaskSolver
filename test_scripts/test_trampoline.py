#!/usr/bin/env python3
"""Smoke test for the full working hook union (gum + cgocall-trampoline).

The shim installs every working hook in one pass — no stage selector. This runs a real
`agy --print` turn under it and asserts the union installs, fires, and does not corrupt
the turn:

  login-independent — the shim installs and a hook fires end-to-end at startup
      (os.Getenv → the gum "smoke" hook, recorded to the capture). A corrupt register
      block shows up as `$HOME is not defined`, a wrong `kind` as a worker
      UnicodeDecodeError, a bad GC unwind as throw("unknown pc").
  login-gated — with the WHOLE union active at once (never exercised under the old
      per-stage runs), the model turn must still complete AND every surface decode from
      the one capture: SendUserMessage/Send (trampoline app-boundary), genai_turn (wire),
      app_response (app boundary), and the StreamGenerateContent RPC (rpc trace).

This is the regression guard for an agy update: after `pixi run shim-symbols`
re-resolves offsets for a new build, run this to confirm the union still works. It is
also the primary check that the combined install doesn't destabilize a turn.

It SKIPS (exit 0), rather than fails, when the environment can't run it — no agy
binary, shim not built, or the running agy's build-id doesn't match symbols.json
(the shim then refuses to hook; re-run `pixi run shim-symbols`). Model-turn
checks are skipped when agy isn't logged in.

    python3 test_scripts/test_trampoline.py            # validates the full union (mixed full-cgo + asmcgo)
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from agy_session import AgySession  # noqa: E402
from pyagy import trust_workspace   # noqa: E402  (pre-trust the PTY workspace)

ROOT = os.path.join(os.path.dirname(HERE), "antigravity")
SHIM = os.path.join(ROOT, "vendor", "antigravity.so")
AGY = os.path.expanduser(os.environ.get("AGY_BIN", "~/.local/bin/agy"))
PROMPT = "Reply with exactly the single word ZORPLE and nothing else."
CRASH = ["throw", "unexpected return pc", "unknown pc", "fatal error", "panic", "SIGSEGV"]


def skip(msg):
    print(f"SKIP: {msg}")
    sys.exit(0)


def run(extra, workdir, timeout=200.0):
    """Run one agy --print turn under the shim to completion; return a dict of signals."""
    label = "-".join(f"{k}={v or 'empty'}" for k, v in sorted(extra.items())) or "union"
    cap = os.path.join(workdir, f"cap_{label}.jsonl")
    log = os.path.join(workdir, f"log_{label}.log")
    for f in (cap, log):
        if os.path.exists(f):
            os.remove(f)
    s = AgySession(agy=AGY, capture=cap, log=log, workdir=workdir, extra_env=extra)
    try:
        s.start(["--print", PROMPT])
        s.collect(timeout=timeout)      # one-shot: drive to completion (drains the PTY + collects turns)
        out = s.transcript              # rendered transcript (ZORPLE + any crash traceback)
    finally:
        s.close()
    logtxt = open(log, errors="replace").read() if os.path.exists(log) else ""
    kinds = collections.Counter()
    genai_text = ""
    if os.path.exists(cap):
        for ln in open(cap):
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            kinds[obj.get("kind")] += 1
            if obj.get("kind") == "genai_turn" and obj.get("text"):
                genai_text += obj["text"]
    combined = out + "\n" + logtxt
    answer = "\n".join(l for l in out.splitlines()
                       if l.strip() and "antigravity" not in l and "wirecap" not in l
                       and "gohook" not in l and "gomod" not in l)
    return {
        "kinds": dict(kinds), "log": logtxt, "genai_text": genai_text,
        "zorple": bool(re.search(r"\bZORPLE\b", answer)),
        "crashes": sum(combined.count(k) for k in CRASH),
        "home_bad": combined.count("HOME is not defined"),
        "unicode_err": combined.count("UnicodeDecodeError"),
    }


def main():
    argparse.ArgumentParser(description="Smoke-test the antigravity hook union").parse_args()

    if not os.path.exists(AGY):
        skip(f"agy not found at {AGY} (set AGY_BIN)")
    if not os.path.exists(SHIM):
        skip(f"shim not built: {SHIM} (run `pixi run build-shim`)")

    wd = tempfile.mkdtemp(prefix="agy_tramp_")
    subprocess.run("git init -q && printf x > f && git add -A && "
                   "git -c user.email=t@t -c user.name=t commit -qm init",
                   shell=True, cwd=wd, check=False)  # agy hangs in a non-git dir
    trust_workspace(wd)   # pre-trust so agy under the PTY doesn't block on the folder-trust prompt

    failures = []

    def check(cond, name):
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    # One run installs the full working hook union (gum wire hooks + trampoline app/rpc
    # hooks). Validate the mechanism login-independently first: install + a hook firing
    # end-to-end + no startup corruption. os.Getenv fires before any auth/network.
    print("[union] full working hook union under one agy --print turn")
    r = run({}, wd)
    if "build-id ok" not in r["log"]:
        skip("shim build-id does not match the running agy — re-run "
             "`pixi run shim-symbols` && `pixi run build-shim` (agy may have auto-updated)")
    check("cgocall-trampoline: installed" in r["log"], "union: trampoline installed")
    check(r["kinds"].get("smoke", 0) > 0, "union: gum os.Getenv hook fired end-to-end")
    # readlink_filter fires at os-package init (os.readlink of /proc/self/exe) every turn — a
    # deterministic, login-independent check of the trampoline FILTER (pass/return) mode: the hook
    # RETURNed the real agy path, skipping the body. If this is 0 the filter mode is inert.
    check(r["kinds"].get("readlink_filter", 0) >= 1,
          "union: readlink_filter fired (os.readlink /proc/self/exe → RETURN, filter mode)")
    check(r["home_bad"] == 0, "union: no $HOME corruption (register block intact)")
    check(r["unicode_err"] == 0, "union: no worker UnicodeDecodeError (kind intact)")
    check(r["crashes"] == 0, "union: no throw/unknown-pc/panic (GC unwind safe)")

    # Auth probe: a plain (no-shim) turn must return ZORPLE, else agy isn't logged in.
    authed = run({"LD_PRELOAD": ""}, wd)["zorple"]
    if not authed:
        print("NOTE: agy not authenticated — skipping model-turn checks "
              "(run `agy` once to sign in to exercise them)")
    else:
        # THE critical assertion: with the ENTIRE union installed at once (gum tls + all
        # trampoline app/rpc hooks — never combined under the old per-stage runs), the
        # model turn still completes AND every surface decodes from the one capture.
        check(r["zorple"], "union: turn completes (ZORPLE) with all hooks active")
        check(r["kinds"].get("send_user_msg", 0) >= 1, "union: SendUserMessage fired (full-cgo trampoline)")
        check(r["kinds"].get("stream_send", 0) >= 1, "union: callbackStreamer.Send fired (asmcgo trampoline)")
        check(r["kinds"].get("http_rt", 0) >= 1, "union: RoundTrip fired (asmcgo trampoline)")
        check(r["kinds"].get("genai_turn", 0) >= 1, "union: genai_turn emitted (wire)")
        check("ZORPLE" in r["genai_text"], "union: decoded wire text is non-empty (ZORPLE)")
        check(r["kinds"].get("app_response", 0) >= 1, "union: app_response decoded (app boundary)")
        check(r["kinds"].get("rpc_stream_generate", 0) >= 1,
              "union: StreamGenerateContent RPC traced (rpc, full-cgo)")

    print()
    if failures:
        print(f"FAIL ({len(failures)}): " + ", ".join(failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
