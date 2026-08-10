#!/usr/bin/env python3
"""Offline end-to-end tests for pycodex's launch machinery using stub binaries
(`#!/bin/sh` scripts passed as codex_bin=) — no codex build, no network.

Pins: the stdin-prompt path round-trips arbitrarily large prompts without echo
into the argv; the drain deadline surfaces as CodexResponse.timed_out; close()
sweeps the CLI's whole process group (codex-spawned MCP/tool grandchildren die);
no PTY master fd leaks across runs.

    python3 test_scripts/test_codex_process.py
"""
import gc
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "codex"))
sys.path.insert(0, _REPO)

try:
    import pygit2  # noqa: F401
except ImportError:  # offline: caller-supplied workspaces never touch pygit2
    import types
    sys.modules["pygit2"] = types.ModuleType("pygit2")

from pycodex.client import ask  # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def _stub(td, name, body):
    """A codex stand-in. Real codex's wirecap bridge immediately reads the mp
    boot payload off WIRE_MP_BOOT_FD; the payload embeds the prompt (the
    process object is pickled whole), so a stub that never reads it would
    deadlock ask() for prompts larger than the pipe buffer. Every stub
    therefore drains the boot fd in the background first."""
    path = os.path.join(td, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n"
                'if [ -n "$WIRE_MP_BOOT_FD" ]; then\n'
                '  eval "cat 0<&$WIRE_MP_BOOT_FD" > /dev/null 2>&1 &\n'
                "fi\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


def test_stdin_roundtrip(td):
    print("[offline] stdin prompt round-trip (>64KB, no argv echo)")
    ws = os.path.join(td, "ws1"); os.makedirs(ws)
    stub = _stub(td, "cat-stub", "cat /dev/stdin")
    prompt = "PROMPT-LINE-" + "x" * 100 + "\n"
    prompt = prompt * 700                      # ~80KB — over pipe capacity
    r = ask(prompt, codex_bin=stub, workspace=ws, timeout=30,
            prompt_via_stdin=True, capture=os.path.join(td, "cap1.jsonl"))
    text = r.transcript.replace("\r\n", "\n").replace("\r", "\n")
    check(text.count("PROMPT-LINE-") == 700, "all prompt lines came back")
    check(r.timed_out is False, "no timeout flagged")
    check(r.exit_status == 0, "clean exit")
    check(r.capture_path == os.path.join(td, "cap1.jsonl"), "capture path honored")
    check(not os.path.exists(os.path.join(ws, "codex-capture.jsonl")),
          "workspace not polluted")


def test_timeout(td):
    print("[offline] drain deadline -> timed_out")
    ws = os.path.join(td, "ws2"); os.makedirs(ws)
    stub = _stub(td, "sleep-stub", "sleep 60")
    t0 = time.monotonic()
    r = ask("p", codex_bin=stub, workspace=ws, timeout=2,
            capture=os.path.join(td, "cap2.jsonl"))
    took = time.monotonic() - t0
    check(r.timed_out is True, "timed_out set")
    check(isinstance(r.exit_status, int) and r.exit_status < 0,
          f"signal exit status ({r.exit_status})")
    check(took < 15, f"returned near the deadline ({took:.1f}s)")


def test_group_sweep(td):
    print("[offline] close() sweeps the process group (grandchildren die)")
    ws = os.path.join(td, "ws3"); os.makedirs(ws)
    pidf = os.path.join(td, "grandchild.pid")
    stub = _stub(td, "grandchild-stub",
                 f'sleep 300 &\necho $! > "{pidf}"\nexec sleep 300')
    r = ask("p", codex_bin=stub, workspace=ws, timeout=2,
            capture=os.path.join(td, "cap3.jsonl"))
    check(r.timed_out is True, "stub timed out as arranged")
    deadline = time.monotonic() + 5
    dead = False
    pid = int(open(pidf).read().strip())
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            dead = True
            break
        time.sleep(0.1)
    check(dead, "backgrounded grandchild reaped within 5s")


def test_no_fd_leak(td):
    print("[offline] no PTY master fd leak across runs")
    ws = os.path.join(td, "ws4"); os.makedirs(ws)
    stub = _stub(td, "true-stub", "exit 0")
    ask("p", codex_bin=stub, workspace=ws, timeout=10,
        capture=os.path.join(td, "warm.jsonl"))
    gc.collect()
    before = len(os.listdir("/proc/self/fd"))
    for i in range(3):
        ask("p", codex_bin=stub, workspace=ws, timeout=10,
            capture=os.path.join(td, f"cap4-{i}.jsonl"))
    gc.collect()
    after = len(os.listdir("/proc/self/fd"))
    check(after <= before, f"fd count stable ({before} -> {after})")


def main():
    with tempfile.TemporaryDirectory() as td:
        test_stdin_roundtrip(td)
        test_timeout(td)
        test_group_sweep(td)
        test_no_fd_leak(td)
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
