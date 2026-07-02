#!/usr/bin/env python3
"""Smoke test for the cgocall-trampoline app-hooks (stages 8/9).

Runs real `agy --print` turns under the LD_PRELOAD shim and asserts the trampoline
installs, fires, and does not corrupt the turn:

  stage 9  os.Getenv via the trampoline  — validates the MECHANISM without a login
           (fires at startup; a corrupt register block shows up as `$HOME is not
           defined`, a wrong `kind` as a worker UnicodeDecodeError, a bad GC unwind
           as throw("unknown pc")).
  stage 8  SendUserMessage + callbackStreamer.Send via the trampoline — the parking
           app-boundary funcs; needs an authenticated agy to complete a model turn.

This is the regression guard for an agy update: after `make -C antigravity symbols`
re-resolves offsets for a new build, run this to confirm the trampoline still works.

It SKIPS (exit 0), rather than fails, when the environment can't run it — no agy
binary, shim not built, or the running agy's build-id doesn't match symbols.json
(the shim then refuses to hook; re-run `make -C antigravity symbols`). Stage-8 turn
checks are skipped when agy isn't logged in.

    python3 test_scripts/test_trampoline.py            # cgocall (default) + baseline
    python3 test_scripts/test_trampoline.py --asmcgo   # also cross-check the asmcgocall variant
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

ROOT = os.path.join(os.path.dirname(HERE), "antigravity")
SHIM = os.path.join(ROOT, "vendor", "antigravity.so")
AGY = os.path.expanduser(os.environ.get("AGY_BIN", "~/.local/bin/agy"))
PROMPT = "Reply with exactly the single word ZORPLE and nothing else."
CRASH = ["throw", "unexpected return pc", "unknown pc", "fatal error", "panic", "SIGSEGV"]


def skip(msg):
    print(f"SKIP: {msg}")
    sys.exit(0)


def run(stage, extra, workdir, idle=25.0, timeout=160.0):
    """Run one agy --print turn; return a dict of signals."""
    label = "-".join(f"{k}={v}" for k, v in sorted(extra.items())) or "plain"
    cap = os.path.join(workdir, f"cap_s{stage}_{label}.jsonl")
    log = os.path.join(workdir, f"log_s{stage}_{label}.log")
    for f in (cap, log):
        if os.path.exists(f):
            os.remove(f)
    s = AgySession(stage=stage, capture=cap, log=log, workdir=workdir, extra_env=extra)
    try:
        s.start(["--print", PROMPT])
        out = s.read_until_idle(idle=idle, timeout=timeout)
    finally:
        s.close()
    logtxt = open(log, errors="replace").read() if os.path.exists(log) else ""
    kinds = collections.Counter()
    if os.path.exists(cap):
        for ln in open(cap):
            try:
                kinds[json.loads(ln).get("kind")] += 1
            except Exception:
                pass
    combined = out + "\n" + logtxt
    answer = "\n".join(l for l in out.splitlines()
                       if l.strip() and "antigravity" not in l
                       and "gohook" not in l and "gomod" not in l)
    return {
        "kinds": dict(kinds), "log": logtxt,
        "zorple": bool(re.search(r"\bZORPLE\b", answer)),
        "crashes": sum(combined.count(k) for k in CRASH),
        "home_bad": combined.count("HOME is not defined"),
        "unicode_err": combined.count("UnicodeDecodeError"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asmcgo", action="store_true",
                    help="also cross-check the asmcgocall variant (AGY_PROC_ASMCGO=1)")
    args = ap.parse_args()

    if not os.path.exists(AGY):
        skip(f"agy not found at {AGY} (set AGY_BIN)")
    if not os.path.exists(SHIM):
        skip(f"shim not built: {SHIM} (run `make -C antigravity`)")

    wd = tempfile.mkdtemp(prefix="agy_tramp_")
    subprocess.run("git init -q && printf x > f && git add -A && "
                   "git -c user.email=t@t -c user.name=t commit -qm init",
                   shell=True, cwd=wd, check=False)  # agy hangs in a non-git dir

    failures = []

    def check(cond, name):
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    # Stage 9: the trampoline mechanism, login-independent (os.Getenv fires at startup).
    print("[stage 9] os.Getenv via cgocall trampoline")
    r9 = run(9, {"AGY_PROC_MODULEDATA": "1"}, wd)
    if "build-id ok" not in r9["log"]:
        skip("shim build-id does not match the running agy — re-run "
             "`make -C antigravity symbols` && `make -C antigravity` (agy may have auto-updated)")
    check("cgocall-trampoline stage: installed" in r9["log"], "stage9: trampoline installed")
    check(r9["kinds"].get("cgt_getenv", 0) > 0, "stage9: os.Getenv hook fired")
    check(r9["home_bad"] == 0, "stage9: no $HOME corruption (register block intact)")
    check(r9["unicode_err"] == 0, "stage9: no worker UnicodeDecodeError (kind intact)")
    check(r9["crashes"] == 0, "stage9: no throw/unknown-pc/panic (GC unwind safe)")

    # Auth probe: a plain (no-shim) turn must return ZORPLE, else agy isn't logged in.
    authed = run(1, {"LD_PRELOAD": ""}, wd)["zorple"]
    if not authed:
        print("NOTE: agy not authenticated — skipping stage-8 model-turn checks "
              "(run `agy` once to sign in to exercise them)")
    else:
        check(r9["zorple"], "stage9: turn completes (ZORPLE) with hook active")
        print("[stage 8] SendUserMessage + callbackStreamer.Send via cgocall trampoline")
        r8 = run(8, {"AGY_PROC_MODULEDATA": "1"}, wd)
        check(r8["kinds"].get("send_user_msg", 0) >= 1, "stage8: SendUserMessage fired")
        check(r8["kinds"].get("stream_send", 0) >= 1, "stage8: callbackStreamer.Send fired")
        check(r8["zorple"], "stage8: parking-func turn completes (ZORPLE)")
        check(r8["crashes"] == 0, "stage8: no throw/unknown-pc/panic")
        check(r8["unicode_err"] == 0, "stage8: no worker UnicodeDecodeError")
        if args.asmcgo:
            print("[stage 8] asmcgocall variant cross-check")
            r8a = run(8, {"AGY_PROC_MODULEDATA": "1", "AGY_PROC_ASMCGO": "1"}, wd)
            check(r8a["zorple"] and r8a["crashes"] == 0,
                  "stage8/asmcgo: completes, no crash (matches cgocall)")

    print()
    if failures:
        print(f"FAIL ({len(failures)}): " + ", ".join(failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
