#!/usr/bin/env python3
"""Offline tests for pykimi argv/env assembly, MCP JSON rendering, and the
home-aware session-store readers. No kimi bundle, no node, no network.

    python3 test_scripts/test_kimi_argv.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Sequence
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_KIMI = os.path.join(_REPO, "kimi")
sys.path.insert(0, _KIMI)
sys.path.insert(0, _REPO)

# pykimi.client imports wirecap.runtime.workspace, whose module-level
# `import pygit2` is only *used* at workspace-creation time — which this offline
# suite never reaches. Stub it when absent so the suite runs without the dep.
try:
    import importlib
    importlib.import_module("pygit2")
except ImportError:
    import types
    sys.modules["pygit2"] = types.ModuleType("pygit2")

from pykimi import config as kimi_config  # noqa: E402
from pykimi import sessions  # noqa: E402
from pykimi._env import WIRE_NODE_ADDON, kimi_argv, linked_env  # noqa: E402

from wirecap.types import JSONObject, json_array, json_loads_object, json_object

_failures: list[str] = []


def check(cond: object, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


BIN = "/fake/kimi"


def test_argv_fresh() -> None:
    print("[offline] kimi_argv: fresh one-shot print mode")
    argv = kimi_argv("hi", kimi_bin=BIN)
    check(argv == [BIN, "-p", "hi"], "fresh -p argv")
    argv = kimi_argv("hi", kimi_bin=BIN, extra_flags=["--output-format", "stream-json"])
    check(argv[-2:] == ["--output-format", "stream-json"], "extra_flags trail")
    argv = kimi_argv("hi")
    check(argv[1].endswith("main.mjs") and argv[2:] == ["-p", "hi"],
          "default = node + vendored main.mjs")


def test_argv_print_mode_rejects() -> None:
    """kimi-code refuses --yolo/-y/--auto/--plan alongside -p and exits 1 with an
    EMPTY stdout, which would surface as an unexplained no-output failure. The
    argv builder raises instead, naming the flag."""
    print("[offline] kimi_argv: flags print mode rejects")
    from pykimi._env import PRINT_MODE_REJECTS
    for flag in PRINT_MODE_REJECTS:
        try:
            kimi_argv("hi", kimi_bin=BIN, extra_flags=[flag])
            check(False, f"{flag} rejected in print mode")
        except ValueError as exc:
            check(flag in str(exc), f"{flag} rejected in print mode")
    # the flags a print-mode run legitimately uses still pass through
    argv = kimi_argv("hi", kimi_bin=BIN,
                     extra_flags=["--output-format", "stream-json", "--skills-dir", "/tmp/s"])
    check(argv[-4:] == ["--output-format", "stream-json", "--skills-dir", "/tmp/s"],
          "legitimate flags unaffected")


def test_argv_resume() -> None:
    print("[offline] kimi_argv: resume flags precede -p")
    sid = "session_0123456789abcdef"
    argv = kimi_argv("more", kimi_bin=BIN, session_id=sid)
    check(argv == [BIN, "-S", sid, "-p", "more"], "resume -S argv")
    argv = kimi_argv("p", kimi_bin=BIN, continue_latest=True)
    check(argv == [BIN, "--continue", "-p", "p"], "--continue argv")
    argv = kimi_argv("p", kimi_bin=BIN, session_id=sid, continue_latest=True)
    check("--continue" not in argv, "session_id wins over continue_latest")


def test_env_contract() -> None:
    print("[offline] linked_env: bridge knobs + KIMI_CODE_HOME/KIMI_MODEL_NAME")
    with tempfile.TemporaryDirectory() as td:
        cap = os.path.join(td, "c.jsonl")
        env = linked_env(cap, base={"CONDA_PREFIX": "/conda"})
        check(env.get("WIRE_ENABLE") == "1" and env.get("WIRE_MODULE") == "pykimi.kimi_process",
              "bridge gate + module")
        check(env.get("WIRE_CAPTURE") == os.path.abspath(cap), "capture absolute")
        check(env.get("WIRE_NODE_ADDON") == os.path.abspath(WIRE_NODE_ADDON),
              "addon path exported")
        check(env.get("WIRE_MAXCOPY") == "8388608", "maxcopy raised for full-history requests")
        check(env.get("PYTHONHOME") == "/conda", "PYTHONHOME from CONDA_PREFIX")
        check(env.get("KIMI_CODE_NO_AUTO_UPDATE") == "1", "auto-update off")
        env = linked_env(cap, base={"PYTHONHOME": "/mine", "CONDA_PREFIX": "/conda"})
        check(env.get("PYTHONHOME") == "/mine", "pre-set PYTHONHOME respected")
        # The interpreter's own prefix is the home when it is a complete environment holding these
        # packages (a Nix python.withPackages environment), even under someone else's CONDA_PREFIX.
        prefix = os.path.join(td, "env")
        lib = os.path.join(prefix, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}")
        os.makedirs(os.path.join(lib, "site-packages"))
        open(os.path.join(lib, "os.py"), "w").close()
        with mock.patch.object(sys, "prefix", prefix):
            check("PYTHONHOME" not in linked_env(cap, base={}), "no PYTHONHOME from a prefix without wirecap")
            os.makedirs(os.path.join(lib, "site-packages", "wirecap"))
            check(linked_env(cap, base={}).get("PYTHONHOME") == prefix, "PYTHONHOME from a complete env's prefix")
            check(linked_env(cap, base={"CONDA_PREFIX": "/conda"}).get("PYTHONHOME") == prefix,
                  "a complete env's prefix wins over another CONDA_PREFIX")


def test_env_home_model_precedence() -> None:
    print("[offline] linked_env: kwargs win over extra_env")
    with tempfile.TemporaryDirectory() as td:
        cap = os.path.join(td, "c.jsonl")
        home = os.path.join(td, "home")
        env = linked_env(cap, base={}, kimi_home=home, model="k3",
                               extra_env={"KIMI_CODE_HOME": "/other",
                                          "KIMI_MODEL_NAME": "other"})
        check(env.get("KIMI_CODE_HOME") == home, "kimi_home kwarg wins")
        check(env.get("KIMI_MODEL_NAME") == "k3", "model kwarg wins")
        env = linked_env(cap, base={}, extra_env={"KIMI_CODE_HOME": "/other",
                                                        "KIMI_MODEL_NAME": "other"})
        check(env.get("KIMI_CODE_HOME") == "/other"
              and env.get("KIMI_MODEL_NAME") == "other", "extra_env alone still works")


def test_mcp_json_wrap() -> None:
    print("[offline] pykimi.config.mcp_json: PYTHONHOME unwrap")
    spec = {"command": "uv", "args": ["run", "blender-mcp"],
            "env": {"BLENDER_MCP_PORT": "9"}}
    doc = json_loads_object(kimi_config.mcp_json({"blender": spec}))
    srv = json_object(json_object(doc["mcpServers"])["blender"])
    check(srv["command"] == "/usr/bin/env" and json_array(srv["args"])[:3] == ["-u", "PYTHONHOME", "uv"],
          "wrapped by default")
    bare = json_loads_object(kimi_config.mcp_json({"blender": spec}, unset_pythonhome=False))
    check(json_object(json_object(bare["mcpServers"])["blender"])["command"] == "uv", "opt-out renders bare")
    from wirecap.runtime.mcp import env_wrapped
    again = kimi_config.mcp_json({"blender": env_wrapped(spec)})
    check(json_loads_object(again) == doc, "idempotent on pre-wrapped specs")


def test_ask_mutual_exclusion() -> None:
    print("[offline] ask: session_id + continue_latest rejected")
    from pykimi import client
    try:
        client.ask("p", session_id="x", continue_latest=True)
        check(False, "ValueError raised")
    except ValueError:
        check(True, "ValueError raised")
    except Exception as exc:  # noqa: BLE001
        check(False, f"ValueError raised (got {type(exc).__name__})")


def _seed_session(home: str, wd_key: str, sid: str, workdir: str, wire_records: Sequence[JSONObject]=(), agent: str="main"):
    sdir = os.path.join(home, "sessions", wd_key, sid)
    adir = os.path.join(sdir, "agents", agent)
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "wire.jsonl"), "w") as f:
        for rec in wire_records:
            f.write(json.dumps(rec) + "\n")
    with open(os.path.join(home, sessions.INDEX_NAME), "a") as f:
        f.write(json.dumps({"sessionId": sid, "sessionDir": sdir,
                            "workDir": workdir}) + "\n")
    return sdir


def test_sessions_store() -> None:
    print("[offline] sessions readers: index fold, tombstones, cwd filter, wire")
    with tempfile.TemporaryDirectory() as td:
        home = os.path.join(td, "home"); os.makedirs(home)
        cwd_a = os.path.join(td, "a"); os.makedirs(cwd_a)
        cwd_b = os.path.join(td, "b"); os.makedirs(cwd_b)
        sid_old = "session_1111"
        sid_new = "session_2222"
        sid_dead = "session_3333"
        _seed_session(home, "wd_a_0001", sid_old, cwd_a,
                      [{"type": "metadata", "protocol_version": "1.5"},
                       {"type": "usage.record", "usage": {"output": 5}}])
        _seed_session(home, "wd_b_0002", sid_new, cwd_b)
        _seed_session(home, "wd_b_0002", sid_dead, cwd_b)
        with open(os.path.join(home, sessions.INDEX_NAME), "a") as f:
            f.write(json.dumps({"sessionId": sid_dead, "deleted": True}) + "\n")
        check(sessions.latest_session_id(home=home) == sid_new,
              "latest live session (tombstone skipped)")
        check(sessions.latest_session_id(home=home, cwd=cwd_a) == sid_old, "cwd filter")
        check(sessions.latest_session_id(home=os.path.join(td, "empty")) is None,
              "empty store -> None")
        session_dir = sessions.find_session_dir(sid_old, home=home)
        check(session_dir is not None and session_dir.endswith(sid_old),
              "find_session_dir via index")
        wire = sessions.read_wire(sid_old, home=home)
        check(len(wire) == 2 and wire[1]["type"] == "usage.record", "read_wire")
        # relocated store: recorded sessionDir is stale, the sessions/ scan finds it
        moved = os.path.join(td, "moved"); os.rename(home, moved)
        moved_session = sessions.find_session_dir(sid_old, home=moved)
        check(moved_session is not None and moved_session.endswith(sid_old),
              "find_session_dir survives a relocated home")


def main() -> None:
    test_argv_fresh()
    test_argv_print_mode_rejects()
    test_argv_resume()
    test_env_contract()
    test_env_home_model_precedence()
    test_mcp_json_wrap()
    test_ask_mutual_exclusion()
    test_sessions_store()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
