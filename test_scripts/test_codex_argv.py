#!/usr/bin/env python3
"""Offline tests for pycodex argv/env assembly, MCP flag rendering, and the
home-aware session-store readers. No codex binary, no network.

    python3 test_scripts/test_codex_argv.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_CODEX = os.path.join(_REPO, "codex")
sys.path.insert(0, _CODEX)
sys.path.insert(0, _REPO)

# pycodex.client imports wirecap.runtime.workspace, whose module-level
# `import pygit2` is only *used* at workspace-creation time — which this offline
# suite never reaches. Stub it when absent so the suite runs without the dep.
try:
    import pygit2  # noqa: F401
except ImportError:
    import types
    sys.modules["pygit2"] = types.ModuleType("pygit2")

from pycodex._env import codex_argv, instrumented_env  # noqa: E402
from pycodex import config as codex_config  # noqa: E402
from pycodex import sessions  # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


BIN = "/fake/codex"
WS = "/tmp/ws"


def test_argv_fresh():
    print("[offline] codex_argv: fresh one-shot")
    argv = codex_argv("hi", WS, model="m", codex_bin=BIN)
    check(argv == [BIN, "exec", "hi", "--skip-git-repo-check", "-C", WS, "-m", "m"],
          "fresh exec argv")
    argv = codex_argv("hi", WS, codex_bin=BIN, extra_flags=["--json"])
    check(argv[-1] == "--json", "extra_flags trail")


def test_argv_stdin():
    print("[offline] codex_argv: prompt_via_stdin")
    argv = codex_argv("a long prompt", WS, codex_bin=BIN, prompt_via_stdin=True)
    check(argv == [BIN, "exec", "-", "--skip-git-repo-check", "-C", WS],
          "positional becomes -")
    argv = codex_argv("p", WS, codex_bin=BIN, persistent=True, prompt_via_stdin=True)
    check(argv == [BIN, "p", "-C", WS], "TUI ignores prompt_via_stdin")


def test_argv_resume_drops_C():
    print("[offline] codex_argv: non-interactive resume drops -C")
    sid = "0123456789abcdef0123456789abcdef"
    argv = codex_argv("more", WS, codex_bin=BIN, session_id=sid)
    check(argv == [BIN, "exec", "resume", sid, "more", "--skip-git-repo-check"],
          "resume <id> argv (no -C)")
    argv = codex_argv(None, WS, codex_bin=BIN, session_id=sid, prompt_via_stdin=True)
    check(argv == [BIN, "exec", "resume", sid, "-", "--skip-git-repo-check"],
          "resume + stdin prompt")
    argv = codex_argv("p", WS, codex_bin=BIN, continue_latest=True)
    check(argv == [BIN, "exec", "resume", "--last", "p", "--skip-git-repo-check"],
          "resume --last (no -C)")
    check("-C" in codex_argv("p", WS, codex_bin=BIN), "fresh keeps -C")


def test_env_codex_home():
    print("[offline] instrumented_env: codex_home injection + precedence")
    with tempfile.TemporaryDirectory() as td:
        env = instrumented_env(os.path.join(td, "c.jsonl"), base={},
                               codex_home=os.path.join(td, "home"))
        check(env.get("CODEX_HOME") == os.path.join(td, "home"), "kwarg injects")
        env = instrumented_env(os.path.join(td, "c.jsonl"), base={},
                               extra_env={"CODEX_HOME": "/other"},
                               codex_home=os.path.join(td, "home"))
        check(env.get("CODEX_HOME") == os.path.join(td, "home"),
              "kwarg wins over extra_env")
        env = instrumented_env(os.path.join(td, "c.jsonl"), base={},
                               extra_env={"CODEX_HOME": "/other"})
        check(env.get("CODEX_HOME") == "/other", "extra_env alone still works")


def test_mcp_flags_wrap():
    print("[offline] pycodex.config.mcp_flags: PYTHONHOME unwrap")
    spec = {"command": "uv", "args": ["run", "blender-mcp"],
            "env": {"BLENDER_MCP_PORT": "9"}}
    flags = codex_config.mcp_flags({"blender": spec})
    check(flags[0] == "-c" and 'command = "/usr/bin/env"' in flags[1]
          and '"-u", "PYTHONHOME", "uv"' in flags[1], "wrapped by default")
    flags2 = codex_config.mcp_flags({"blender": spec}, unset_pythonhome=False)
    check('command = "uv"' in flags2[1], "opt-out renders bare")
    # idempotent: feeding wrapped specs back does not double-wrap
    from wirecap.runtime.mcp import env_wrapped
    again = codex_config.mcp_flags({"blender": env_wrapped(spec)})
    check(again == flags, "idempotent on pre-wrapped specs")


def test_ask_mutual_exclusion():
    print("[offline] ask: session_id + continue_latest rejected")
    from pycodex import client
    try:
        client.ask("p", session_id="x", continue_latest=True)
        check(False, "ValueError raised")
    except ValueError:
        check(True, "ValueError raised")
    except Exception as exc:  # noqa: BLE001
        check(False, f"ValueError raised (got {type(exc).__name__})")


def _rollout(root, day, ts, sid, cwd, when):
    d = os.path.join(root, "sessions", day)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"rollout-{ts}-{sid}.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"timestamp": ts, "type": "session_meta",
                            "payload": {"session_id": sid, "cwd": cwd}}) + "\n")
        f.write(json.dumps({"timestamp": ts, "type": "response_item",
                            "payload": {"role": "assistant", "type": "message",
                                        "content": [{"text": f"hello from {sid}"}]}}) + "\n")
    os.utime(path, (when, when))
    return path


def test_sessions_home_aware():
    print("[offline] sessions readers honor home=")
    with tempfile.TemporaryDirectory() as td:
        home = os.path.join(td, "home")
        cwd_a = os.path.join(td, "a"); os.makedirs(cwd_a)
        cwd_b = os.path.join(td, "b"); os.makedirs(cwd_b)
        sid_old = "11111111-1111-1111-1111-111111111111"
        sid_new = "22222222-2222-2222-2222-222222222222"
        _rollout(home, "2026/08/10", "2026-08-10T10-00-00", sid_old, cwd_a, 1000)
        _rollout(home, "2026/08/10", "2026-08-10T11-00-00", sid_new, cwd_b, 2000)
        check(sessions.latest_session_id(home=home) == sid_new, "newest wins")
        check(sessions.latest_session_id(home=home, cwd=cwd_a) == sid_old,
              "cwd filter")
        check(sessions.find_rollout(sid_old, home=home).endswith(f"{sid_old}.jsonl"),
              "find_rollout(home=)")
        tr = sessions.read_transcript(sid_new, home=home)
        check(len(tr) == 1 and tr[0]["content"] == f"hello from {sid_new}",
              "read_transcript(home=)")
        check(sessions.latest_session_id(home=os.path.join(td, "empty")) is None,
              "empty store -> None")


def main():
    test_argv_fresh()
    test_argv_stdin()
    test_argv_resume_drops_C()
    test_env_codex_home()
    test_mcp_flags_wrap()
    test_ask_mutual_exclusion()
    test_sessions_home_aware()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
