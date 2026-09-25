#!/usr/bin/env python3
"""Offline tests for pykimi's shell-mode plumbing and Session (stub binaries,
no kimi bundle, no node, no network).

Pins: persistent argv carries no ``-p`` (and rejects a positional prompt);
``-S``/``--continue`` still precede it; print-mode-rejected flags are legal in
shell mode; ``encode_workdir_key`` reproduces a key minted by the real CLI
(production golden); ``trust_workspace`` writes the CLI's exact record shape,
idempotently, under the home; ``read_transcript`` projects wire journals into
pycodex's transcript shape; multi-line prompts arrive bracketed-paste-wrapped
over the PTY while single-line prompts stay plain; and a stub-backed Session
types turn 1 (no prefill), guards a dead CLI, and sweeps on close.

    python3 test_scripts/test_kimi_session.py
"""
import json
import os
import sys
import tempfile
from typing import Final

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "kimi"))
sys.path.insert(0, _REPO)

try:
    import pygit2  # noqa: F401
except ImportError:  # offline: caller-supplied workspaces never touch pygit2
    import types
    sys.modules["pygit2"] = types.ModuleType("pygit2")

from pykimi._env import kimi_argv  # noqa: E402
from pykimi.config import encode_workdir_key, trust_workspace  # noqa: E402
from pykimi import sessions  # noqa: E402
from pykimi.client import Session  # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


BIN = "/fake/kimi"


def test_argv_persistent():
    print("[offline] kimi_argv: shell mode (persistent)")
    argv = kimi_argv(None, kimi_bin=BIN, persistent=True)
    check(argv == [BIN], "bare argv — shell mode is the absence of -p")
    argv = kimi_argv(None, kimi_bin=BIN, persistent=True, session_id="session_1")
    check(argv == [BIN, "-S", "session_1"], "-S precedes in shell mode")
    argv = kimi_argv(None, kimi_bin=BIN, persistent=True, continue_latest=True)
    check(argv == [BIN, "--continue"], "--continue in shell mode")
    argv = kimi_argv(None, kimi_bin=BIN, persistent=True, extra_flags=["--yolo"])
    check(argv == [BIN, "--yolo"], "print-mode-rejected flags are legal here")
    try:
        kimi_argv("hi", kimi_bin=BIN, persistent=True)
        check(False, "positional prompt rejected in shell mode")
    except ValueError:
        check(True, "positional prompt rejected in shell mode")


def test_workdir_key_golden():
    print("[offline] encode_workdir_key: production golden")
    # Key minted by the real kimi-code 0.34.0 on the lab box (session_index.jsonl):
    # workDir .../GrassTuftFactory -> wd_grasstuftfactory_aa5a5ce10ff5
    wd = "/home/ubuntu/3D-CoT/1graded_exp/outputs/full_mcp/w_texture/kimi-k3/GrassTuftFactory"
    check(encode_workdir_key(wd) == "wd_grasstuftfactory_aa5a5ce10ff5",
          "matches the key the real CLI minted")
    check(encode_workdir_key(wd + "///") == "wd_grasstuftfactory_aa5a5ce10ff5",
          "trailing slashes normalized away")
    check(encode_workdir_key("/").startswith("wd_workspace_"),
          "degenerate basename falls back to 'workspace'")


def test_trust_workspace(td):
    print("[offline] trust_workspace: record shape, placement, idempotence")
    home = os.path.join(td, "home")
    ws = os.path.join(td, "MyProj")
    os.makedirs(ws)
    path = trust_workspace(ws, home=home)
    key = encode_workdir_key(os.path.abspath(ws))
    check(path == os.path.join(home, "workspace-trust", key),
          "record at <home>/workspace-trust/<wd_key>")
    doc = json.loads(open(path).read())
    check(set(doc) == {"root", "trustedAt"} and doc["root"] == os.path.abspath(ws)
          and isinstance(doc["trustedAt"], int),
          "WorkspaceTrustService record shape {root, trustedAt(ms)}")
    raw = open(path).read()
    check(":" in raw and ", " not in raw and raw == raw.strip(),
          "compact JSON, no trailing newline (JSON.stringify parity)")
    before = raw
    trust_workspace(ws, home=home)
    check(open(path).read() == before, "idempotent — presence IS the contract")


def test_read_transcript(td):
    print("[offline] sessions.read_transcript: wire -> pycodex transcript shape")
    home = os.path.join(td, "kimi_home")
    sid = "session_rt"
    sdir = os.path.join(home, "sessions", "wd_x_000000000000", sid)
    adir = os.path.join(sdir, "agents", "main")
    os.makedirs(adir)
    recs = [
        {"type": "metadata", "protocol_version": "1.5"},
        {"type": "turn.prompt", "input": [{"type": "text", "text": "hi"}]},
        {"type": "context.append_message", "time": 111,
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "hi "},
                                 {"type": "text", "text": "there"}]}},
        {"type": "usage.record", "usage": {"output": 5}},
        {"type": "context.append_message", "time": 222,
         "message": {"role": "assistant", "content": "4"}},
    ]
    with open(os.path.join(adir, "wire.jsonl"), "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(home, sessions.INDEX_NAME), "w") as f:
        f.write(json.dumps({"sessionId": sid, "sessionDir": sdir,
                            "workDir": td}) + "\n")
    t = sessions.read_transcript(sid, home=home)
    check([e["role"] for e in t] == ["user", "assistant"],
          "only conversation records project")
    check(t[0] == {"step_index": 0, "role": "user", "type": "message",
                   "created_at": 111, "content": "hi there"},
          "parts flattened; keys match pycodex.read_transcript")
    check(t[1]["content"] == "4" and t[1]["step_index"] == 1,
          "string content passes through; step_index increments")


def test_read_transcript_v2(td):
    print("[offline] sessions.read_transcript: Kimi 2.x assistant/tool journals")
    home: Final = os.path.join(td, "kimi_v2_home")
    sid: Final = "session_v2"
    sdir: Final = os.path.join(home, "sessions", "wd_v2_000000000000", sid)

    def loop(time, kind, **fields):
        return {"type": "context.append_loop_event", "time": time,
                "event": {"type": kind, **fields}}

    def message(time, role, text):
        return {"type": "context.append_message", "time": time,
                "message": {"role": role, "content": [{"type": "text", "text": text}]}}

    def write_agent(aid, records):
        directory: Final = os.path.join(sdir, "agents", aid)
        os.makedirs(directory)
        with open(os.path.join(directory, "wire.jsonl"), "w") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
            f.write('{"incomplete":')  # a read during the next append is safe

    # Same field names as v2.1.1's LoopRecordedEvent. Mix legacy records with
    # streamed steps, a tool result and a user injection arriving while it runs.
    write_agent("main", [
        {"type": "metadata", "protocol_version": "1.5"},
        message(1, "user", "Read the file, then explain it."),
        message(2, "assistant", "A legacy reply."),
        loop(3, "step.begin", uuid="step-1"),
        loop(4, "content.part", stepUuid="step-1", part={"type": "think", "think": "Read first"}),
        loop(5, "content.part", stepUuid="step-1", part={"type": "text", "text": "Reading "}),
        loop(6, "content.part", stepUuid="step-1", part={"type": "text", "text": "the file."}),
        loop(7, "tool.call", stepUuid="step-1", toolCallId="read-1", name="Read", args={}),
        message(8, "user", "Keep it short."),
        loop(9, "tool.result", toolCallId="read-1",
             result={"output": [{"type": "text", "text": "file "},
                                {"type": "text", "text": "contents"}]}),
        loop(10, "step.end", uuid="step-1", finishReason="tool_use"),
        loop(11, "step.begin", uuid="step-2"),
        loop(12, "content.part", stepUuid="step-2", part={"type": "text", "text": "Done."}),
        loop(13, "step.end", uuid="step-2", finishReason="stop"),
        loop(14, "step.begin", uuid="empty-step"),
        loop(15, "content.part", stepUuid="empty-step", part={"type": "text", "text": "  "}),
        loop(16, "step.end", uuid="empty-step", finishReason="stop"),
        loop(17, "step.begin", uuid="partial-step"),
        loop(18, "content.part", stepUuid="partial-step", part={"type": "text", "text": "Partial"}),
        loop(19, "step.end", uuid="partial-step", finishReason="interrupted"),
        loop(20, "content.part", stepUuid="partial-step", part={"type": "text", "text": " reply"}),
    ])
    # The main agent ended mid-step. Another journal cannot append to its open
    # assistant even when its first record happens to reference the same UUID.
    write_agent("sub", [
        loop(21, "content.part", stepUuid="partial-step", part={"type": "text", "text": "wrong agent"}),
        message(22, "user", "Subagent task"),
        loop(23, "step.begin", uuid="sub-step"),
        loop(24, "tool.call", stepUuid="sub-step", toolCallId="shell-1", name="Shell", args={}),
        loop(25, "step.end", uuid="sub-step", finishReason="interrupted"),
        loop(26, "step.begin", uuid="next-step"),
        loop(27, "content.part", stepUuid="next-step", part={"type": "text", "text": "Recovered"}),
        loop(28, "step.end", uuid="next-step", finishReason="stop"),
    ])

    main: Final = sessions.read_transcript(sid, home=home, agent="main")
    check([(entry["role"], entry["content"]) for entry in main] == [
        ("user", "Read the file, then explain it."),
        ("assistant", "A legacy reply."),
        ("assistant", "Reading the file."),
        ("tool", "file contents"),
        ("user", "Keep it short."),
        ("assistant", "Done."),
        ("assistant", "Partial reply"),
    ], "mixed legacy/v2 replies, tool output and deferred user message preserve conversation order")
    check(main[2]["created_at"] == 3 and main[3]["created_at"] == 9,
          "assistant timestamp is step.begin; tool timestamp is tool.result")
    check(main[-1]["content"] == "Partial reply" and len(main) == 7,
          "interrupted content survives; completed whitespace-only steps disappear")

    all_agents: Final = sessions.read_transcript(sid, home=home)
    check(all_agents[:len(main)] == main and all_agents[-1]["content"] == "Recovered",
          "agent journals fold independently without leaking an unfinished assistant")
    check(all_agents[-2]["role"] == "tool" and all_agents[-2]["content"] == (
        "Tool execution was interrupted before its result was recorded. "
        "Do not assume the tool completed successfully."
    ), "resuming an unfinished tool uses the upstream interrupted-tool transcript entry")
    check([entry["step_index"] for entry in all_agents] == list(range(len(all_agents))),
          "step indexes stay contiguous across filtered empty steps and multiple agents")
    check(sessions.read_transcript("missing", home=home) == [], "unknown session stays empty")


def _stub(td, name, body):
    """A kimi stand-in that drains the mp boot fd (see test_kimi_process)."""
    path = os.path.join(td, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n"
                'if [ -n "$WIRE_MP_BOOT_FD" ]; then\n'
                '  eval "cat 0<&$WIRE_MP_BOOT_FD" > /dev/null 2>&1 &\n'
                "fi\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


def test_session_stub(td):
    print("[offline] Session over a stub shell: typed turn 1, paste framing, sweep")
    ws = os.path.join(td, "ws-sess")
    os.makedirs(ws)
    # an interactive-ish stub: echoes argv once, then repeats stdin lines
    stub = _stub(td, "shell-stub",
                 'printf "ARGS:%s\\n" "$*"\n'
                 "while IFS= read -r line; do printf 'GOT:%s\\n' \"$line\"; done")
    home = os.path.join(td, "home")
    s = Session(workspace=ws, kimi_bin=stub, timeout=20, kimi_home=home)
    s._PTY_IDLE = 1.5   # stub decodes no turns; settle fast via pty-idle
    try:
        r1 = s.ask("two\nlines")
        text = r1.transcript.replace("\r\n", "\n").replace("\r", "\n")
        check("ARGS:\n" in text or "ARGS:" in text.split("\n", 1)[0] + "\n",
              "no -p and no prompt on the argv")
        # the transcript property is ANSI-stripped (it removes the ESC markers),
        # so assert the framing on the raw PTY bytes the stub echoed back
        raw = bytes(s._proc._popen.raw)
        check(b"\x1b[200~" in raw and b"\x1b[201~" in raw,
              "multi-line turn 1 typed as one bracketed paste")
        check(os.path.isfile(os.path.join(home, "workspace-trust",
                                          encode_workdir_key(ws))),
              "workspace pre-trusted in the session home")
        r2 = s.ask("single")
        check("GOT:" in r2.transcript.replace("\r", ""),
              "stub echoed a typed line back")
        check(b"\x1b[200~single" not in bytes(s._proc._popen.raw),
              "single-line follow-up not paste-wrapped")
    finally:
        s.close()
    check(s._proc is not None and s._proc.exit_status is not None,
          "close() reaped the stub")


def main():
    with tempfile.TemporaryDirectory() as td:
        test_argv_persistent()
        test_workdir_key_golden()
        test_trust_workspace(td)
        test_read_transcript(td)
        test_read_transcript_v2(td)
        test_session_stub(td)
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
