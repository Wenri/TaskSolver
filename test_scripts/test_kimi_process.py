#!/usr/bin/env python3
"""Offline end-to-end tests for pykimi's launch machinery using stub binaries
(`#!/bin/sh` scripts passed as kimi_bin=) — no kimi bundle, no node, no network.

Pins: the argv prompt reaches the CLI and its output comes back through the PTY
transcript; the drain deadline surfaces as KimiResponse.timed_out; close() sweeps
the CLI's whole process group (kimi-spawned MCP/shell grandchildren die); no PTY
master fd leaks across runs.

    python3 test_scripts/test_kimi_process.py
"""
import gc
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "kimi"))
sys.path.insert(0, _REPO)

try:
    import pygit2  # noqa: F401
except ImportError:  # offline: caller-supplied workspaces never touch pygit2
    import types
    sys.modules["pygit2"] = types.ModuleType("pygit2")

from pykimi.client import ask  # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def _stub(td, name, body):
    """A kimi stand-in. The real CLI's wiretap addon immediately reads the mp
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


def test_argv_prompt_roundtrip(td):
    print("[offline] argv prompt reaches the CLI; env contract visible to it")
    ws = os.path.join(td, "ws1"); os.makedirs(ws)
    stub = _stub(td, "echo-stub",
                 'printf "PROMPT=%s\\n" "$2"\n'
                 'printf "HOME=%s MODEL=%s\\n" "$KIMI_CODE_HOME" "$KIMI_MODEL_NAME"')
    home = os.path.join(td, "kimi_home")
    r = ask("what is 2+2?", kimi_bin=stub, workspace=ws, timeout=30, model="k3",
            kimi_home=home, capture=os.path.join(td, "cap1.jsonl"))
    text = r.transcript.replace("\r\n", "\n").replace("\r", "\n")
    check("PROMPT=what is 2+2?" in text, "prompt delivered via -p")
    check(f"HOME={home} MODEL=k3" in text, "KIMI_CODE_HOME + KIMI_MODEL_NAME set")
    check(r.timed_out is False and r.exit_status == 0, "clean exit, no timeout")
    check(r.capture_path == os.path.join(td, "cap1.jsonl"), "capture path honored")
    check(not os.path.exists(os.path.join(ws, "kimi-capture.jsonl")),
          "workspace not polluted")


def test_mcp_json_written(td):
    print("[offline] mcp_servers -> workspace-local .kimi-code/mcp.json")
    ws = os.path.join(td, "ws-mcp"); os.makedirs(ws)
    stub = _stub(td, "true-stub-mcp", "exit 0")
    ask("p", kimi_bin=stub, workspace=ws, timeout=10,
        mcp_servers={"blender": {"command": "uv", "args": ["run", "x"]}},
        capture=os.path.join(td, "cap-mcp.jsonl"))
    import json
    cfg = json.load(open(os.path.join(ws, ".kimi-code", "mcp.json")))
    check(cfg["mcpServers"]["blender"]["command"] == "/usr/bin/env",
          "written + PYTHONHOME-wrapped")


def test_print_mode_reject_is_not_silent(td):
    """The regression that shipped: `kimi -p --yolo` is rejected by the CLI, which
    exits 1 printing only to stderr — so a caller saw an empty answer with no clue
    why. This stub mimics that validator; the guard must stop the call before it
    ever launches."""
    print("[offline] print-mode-rejected flags never reach the CLI")
    ws = os.path.join(td, "ws-reject"); os.makedirs(ws)
    stub = _stub(td, "validator-stub",
                 'for a in "$@"; do\n'
                 '  case "$a" in --yolo|-y|--auto|--plan)\n'
                 '    echo "error: Cannot combine --prompt with $a" >&2; exit 1;; esac\n'
                 'done\n'
                 'echo ok')
    try:
        ask("p", kimi_bin=stub, workspace=ws, timeout=10, extra_flags=["--yolo"],
            capture=os.path.join(td, "cap-reject.jsonl"))
        check(False, "--yolo raises before launch")
    except ValueError as exc:
        check("--yolo" in str(exc), "--yolo raises before launch")
    # and the stub really would have failed the run, proving the guard is load-bearing
    r = ask("p", kimi_bin=stub, workspace=ws, timeout=10,
            capture=os.path.join(td, "cap-ok.jsonl"))
    check(r.exit_status == 0, "same stub succeeds without the rejected flag")


def test_timeout(td):
    print("[offline] drain deadline -> timed_out")
    ws = os.path.join(td, "ws2"); os.makedirs(ws)
    stub = _stub(td, "sleep-stub", "sleep 60")
    t0 = time.monotonic()
    r = ask("p", kimi_bin=stub, workspace=ws, timeout=2,
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
    r = ask("p", kimi_bin=stub, workspace=ws, timeout=2,
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
    ask("p", kimi_bin=stub, workspace=ws, timeout=10,
        capture=os.path.join(td, "warm.jsonl"))
    gc.collect()
    before = len(os.listdir("/proc/self/fd"))
    for i in range(3):
        ask("p", kimi_bin=stub, workspace=ws, timeout=10,
            capture=os.path.join(td, f"cap4-{i}.jsonl"))
    gc.collect()
    after = len(os.listdir("/proc/self/fd"))
    check(after <= before, f"fd count stable ({before} -> {after})")


def main():
    with tempfile.TemporaryDirectory() as td:
        test_argv_prompt_roundtrip(td)
        test_mcp_json_written(td)
        test_print_mode_reject_is_not_silent(td)
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
