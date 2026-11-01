#!/usr/bin/env python3
"""Offline end-to-end tests for pycodex's launch machinery using stub binaries
(`#!/bin/sh` scripts passed as codex_bin=) — no codex build, no network.

Pins: the stdin-prompt path round-trips arbitrarily large prompts without echo
into the argv; the drain deadline surfaces as CodexResponse.timed_out; close()
sweeps the CLI's whole process group (codex-spawned MCP/tool grandchildren die);
no PTY master fd leaks across runs.

    python3 test_scripts/test_codex_process.py
"""

from __future__ import annotations

import errno
import gc
import os
import select
import sys
import tempfile
import time
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "codex"))
sys.path.insert(0, _REPO)

try:
    import importlib
    importlib.import_module("pygit2")
except ImportError:  # offline: caller-supplied workspaces never touch pygit2
    import types
    sys.modules["pygit2"] = types.ModuleType("pygit2")

from pycodex.client import ask  # noqa: E402
from pycodex.codexprocess import CodexPopen, CodexProcess
from wirecap.runtime.pty import WirePtyPopen, service_many
from wirecap.runtime.session import close_channel, new_channel

_failures: list[str] = []


def check(cond: object, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def _stub(td: str, name: str, body: str):
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


def _short_pty_read(popen: CodexPopen) -> bytes:
    read_fd = os.read

    def read(fd: int, size: int) -> bytes:
        return read_fd(fd, min(size, 128) if fd == popen.fd else size)

    # PTY reads may be short even when more output remains queued at child exit.
    with patch("wirecap.runtime.pty.os.read", side_effect=read):
        return WirePtyPopen._read_available(popen)


def test_stdin_roundtrip(td: str) -> None:
    print("[offline] stdin prompt round-trip (>64KB, no argv echo)")
    ws = os.path.join(td, "ws1"); os.makedirs(ws)
    stub = _stub(td, "cat-stub", "cat /dev/stdin")
    prompt = "PROMPT-LINE-" + "x" * 100 + "\n"
    prompt = prompt * 700                      # ~80KB — over pipe capacity
    with patch.object(CodexPopen, "_read_available", _short_pty_read):
        r = ask(prompt, codex_bin=stub, workspace=ws, timeout=30,
                prompt_via_stdin=True, capture=os.path.join(td, "cap1.jsonl"))
    text = r.transcript.replace("\r\n", "\n").replace("\r", "\n")
    check(text.count("PROMPT-LINE-") == 700, "all prompt lines came back")
    check(r.timed_out is False, "no timeout flagged")
    check(r.exit_status == 0, "clean exit")
    check(r.capture_path == os.path.join(td, "cap1.jsonl"), "capture path honored")
    check(not os.path.exists(os.path.join(ws, "codex-capture.jsonl")),
          "workspace not polluted")


def test_multiplex_tail(td: str) -> None:
    print("[offline] multiplexed result readiness drains queued short PTY reads")
    output = "tail-output-" * 500
    stub = _stub(td, "multiplex-tail-stub", f"printf '%s' '{output}'")
    queue = new_channel()
    queue.put({"kind": "fixture"})
    process = CodexProcess("p", workdir=td, codex_bin=stub, capture=os.devnull,
                           args=(queue, ("codex_turn",), None))
    try:
        process.start()
        queue._writer.close()
        process.join(timeout=2)
        assert process._popen is not None
        started = time.monotonic()
        with patch.object(CodexPopen, "_read_available", _short_pty_read):
            ready = service_many([process._popen], [queue._reader], 1)
        elapsed = time.monotonic() - started
        check(ready == [queue._reader], "result readiness is preserved")
        check(process.transcript == output, "multiplexed collector retains every queued byte")
        check(elapsed < 0.5, "a surviving boot-reader holding the slave cannot block the drain")
    finally:
        process.close(grace=0.1)
        close_channel(queue)


def test_timeout(td: str) -> None:
    print("[offline] drain deadline -> timed_out")
    ws = os.path.join(td, "ws2"); os.makedirs(ws)
    stub = _stub(td, "sleep-stub", "sleep 60")
    t0 = time.monotonic()
    r = ask("p", codex_bin=stub, workspace=ws, timeout=2,
            capture=os.path.join(td, "cap2.jsonl"))
    took = time.monotonic() - t0
    check(r.timed_out is True, "timed_out set")
    check(type(r.exit_status) is int and r.exit_status < 0,
          f"signal exit status ({r.exit_status})")
    check(took < 15, f"returned near the deadline ({took:.1f}s)")


def test_capture_disabled(td: str) -> None:
    print("[offline] capture=False preserves existing workspace capture")
    ws = os.path.join(td, "ws-no-capture")
    os.makedirs(ws)
    capture = os.path.join(ws, "codex-capture.jsonl")
    with open(capture, "w") as saved:
        saved.write("previous capture\n")
    stub = _stub(td, "capture-disabled-stub", 'echo "$WIRE_CAPTURE"\necho discarded >> "$WIRE_CAPTURE"')
    result = ask("p", codex_bin=stub, workspace=ws, timeout=5, capture=False)
    check(os.devnull in result.transcript, "native capture is redirected to the null device")
    check(result.capture_path == "", "response reports capture disabled")
    with open(capture) as saved:
        check(saved.read() == "previous capture\n", "existing capture remains unchanged")


def test_group_sweep(td: str) -> None:
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


def test_pidfd_fallback(td: str) -> None:
    print("[offline] missing pidfd polls child exit while a grandchild retains the PTY")
    ws = os.path.join(td, "ws-no-pidfd")
    os.makedirs(ws)
    pidf = os.path.join(td, "no-pidfd-grandchild.pid")
    stub = _stub(td, "no-pidfd-stub",
                 f'trap "" HUP\nsleep 60 &\necho $! > "{pidf}"\n'
                 'echo before-delay\nsleep 0.2\necho after-delay\nexit 7')
    started = time.monotonic()
    with patch("os.pidfd_open", side_effect=OSError(errno.ENOSYS, "pidfds unavailable"), create=True):
        r = ask("p", codex_bin=stub, workspace=ws, timeout=8,
                capture=os.path.join(td, "no-pidfd.jsonl"))
    elapsed = time.monotonic() - started
    check("before-delay" in r.transcript and "after-delay" in r.transcript,
          "PTY output is drained without being mistaken for process death")
    check(not r.timed_out and r.exit_status == 7,
          "polling detects and preserves the leader's exit status without PTY EOF")
    check(elapsed < 6, f"leader exit returns before the deadline ({elapsed:.1f}s)")
    pid = int(open(pidf).read().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            check(True, "fallback close sweeps the grandchild holding the PTY")
            break
        time.sleep(0.1)
    else:
        check(False, "fallback close sweeps the grandchild holding the PTY")


def test_join_timeout_with_pty_output(td: str) -> None:
    print("[offline] join(timeout) remains bounded when live-child PTY output is ready")
    ws = os.path.join(td, "ws-join")
    os.makedirs(ws)
    stub = _stub(td, "join-stub", "echo ready\nsleep 1.5\nexit 7")
    proc = CodexProcess("p", workdir=ws, codex_bin=stub,
                        capture=os.path.join(td, "join.jsonl"))
    with patch("os.pidfd_open", side_effect=OSError(errno.ENOSYS, "pidfds unavailable"), create=True):
        proc.start()
    try:
        assert proc._popen is not None
        assert select.select([proc._popen.fd], [], [], 2)[0], "stub produced no PTY output"
        started = time.monotonic()
        proc.join(timeout=0.05)
        elapsed = time.monotonic() - started
        check(elapsed < 0.5, f"join honors its timeout despite readable output ({elapsed:.3f}s)")
        check(proc.exitcode is None, "short join leaves the live child running")
        proc.join(timeout=4)
        check(proc.exitcode == 7, "later join returns the child's exit status")
    finally:
        proc.close()


def test_no_fd_leak(td: str) -> None:
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


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        test_stdin_roundtrip(td)
        test_multiplex_tail(td)
        test_timeout(td)
        test_capture_disabled(td)
        test_group_sweep(td)
        test_pidfd_fallback(td)
        test_join_timeout_with_pty_output(td)
        test_no_fd_leak(td)
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
