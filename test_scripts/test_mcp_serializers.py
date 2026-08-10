#!/usr/bin/env python3
"""Tests for the shared MCP provisioning layer (wirecap/runtime/mcp.py + the
pyagy write_mcp_servers / prepare_scoped_home extensions).

Offline (no CLI, no network): spec normalization + env_wrapped idempotence; the
per-CLI serializations (codex -c flags incl. TOML escaping + bare-key rejection,
claude config file + flags, qwen settings fragment, kimi json/toml); the generic
mcpServers writer's merge semantics; pyagy's write_mcp_servers/remove_mcp_servers
round-trip against a scoped path; prepare_scoped_home(link_global_config=False)
creating a REAL config dir and seeding the onboarding cache.

    python3 test_scripts/test_mcp_serializers.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)                       # repo root holds the shared `wirecap` package
_ANTIGRAVITY = os.path.join(_REPO, "antigravity")
sys.path.insert(0, _ANTIGRAVITY)
sys.path.insert(0, _REPO)

# pyagy.conversations imports wirecap.runtime.workspace, whose module-level
# `import pygit2` is only *used* at workspace-creation time — which this offline
# test never does. Stub it when absent so the suite runs without the dep.
try:
    import pygit2  # noqa: F401
except ImportError:
    import types
    sys.modules["pygit2"] = types.ModuleType("pygit2")

from wirecap.runtime import mcp  # noqa: E402
from pyagy import config  # noqa: E402
from pyagy import conversations  # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


SPEC = {"command": "/usr/bin/uv",
        "args": ["run", "--directory", "/repo/BlenderMCP/official/mcp", "blender-mcp"],
        "env": {"BLENDER_MCP_HOST": "127.0.0.1", "BLENDER_MCP_PORT": "9999"}}


def test_normalize():
    print("[offline] normalize_server_spec")
    out = mcp.normalize_server_spec({"command": "x"})
    check(out == {"command": "x", "args": [], "env": {}}, "defaults filled")
    out = mcp.normalize_server_spec({"command": "x", "args": [1], "env": {"P": 2}})
    check(out["args"] == ["1"] and out["env"] == {"P": "2"}, "coerced to strings")
    try:
        mcp.normalize_server_spec({"args": []})
        check(False, "missing command rejected")
    except ValueError:
        check(True, "missing command rejected")


def test_env_wrapped():
    print("[offline] env_wrapped idempotence")
    once = mcp.env_wrapped(SPEC)
    check(once["command"] == "/usr/bin/env"
          and once["args"][:2] == ["-u", "PYTHONHOME"]
          and once["args"][2] == SPEC["command"]
          and once["args"][3:] == SPEC["args"]
          and once["env"] == SPEC["env"], "wrap shape")
    check(mcp.env_wrapped(once) == once, "idempotent")
    check(mcp.env_wrapped(SPEC, unset=()) == mcp.normalize_server_spec(SPEC),
          "no vars = plain normalize")


def test_codex_flags():
    print("[offline] codex_config_flags")
    flags = mcp.codex_config_flags({"official-blender-mcp": SPEC})
    check(flags[0] == "-c" and flags[1].startswith("mcp_servers.official-blender-mcp="),
          "dotted key")
    rendered = flags[1].split("=", 1)[1]
    check('"/usr/bin/uv"' in rendered and 'BLENDER_MCP_PORT = "9999"' in rendered,
          "inline TOML table")
    # strings go through JSON escaping so quoted paths survive
    tricky = {"command": 'a"b', "args": [], "env": {}}
    val = mcp.codex_config_flags({"s": tricky})[1]
    check('\\"' in val, "quotes escaped")
    try:
        mcp.codex_config_flags({"bad name": SPEC})
        check(False, "non-bare key rejected")
    except ValueError:
        check(True, "non-bare key rejected")


def test_claude_args():
    print("[offline] claude_mcp_args")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "mcp-servers.json")
        args = mcp.claude_mcp_args({"blender": SPEC}, path)
        check(args == ["--mcp-config", path, "--strict-mcp-config"], "flags")
        doc = json.load(open(path))
        check(doc == {"mcpServers": {"blender": mcp.normalize_server_spec(SPEC)}},
              "file content")
        # byte-parity with the historical harness writer
        legacy = json.dumps({"mcpServers": {"blender": SPEC}}, indent=2)
        check(open(path).read() == legacy, "byte-identical to json.dumps(indent=2)")
        args = mcp.claude_mcp_args({"blender": SPEC}, path, strict=False)
        check(args == ["--mcp-config", path], "strict off")


def test_qwen_fragment():
    print("[offline] qwen_mcp_servers")
    frag = mcp.qwen_mcp_servers({"blender": SPEC})
    check(frag["blender"]["trust"] is True and frag["blender"]["timeout"] == 60000,
          "trust+timeout added")
    check(frag["blender"]["command"] == SPEC["command"], "spec preserved")
    # byte-parity with the historical inline comprehension
    legacy = {name: {**spec, "trust": True, "timeout": 60000}
              for name, spec in {"blender": SPEC}.items()}
    check(json.dumps(frag, indent=2) == json.dumps(legacy, indent=2),
          "byte-identical fragment")


def test_kimi():
    print("[offline] kimi serializations")
    check(json.loads(mcp.kimi_mcp_json({"b": SPEC}))["mcpServers"]["b"]["command"]
          == SPEC["command"], "mcp.json shape")
    lines = mcp.kimi_mcp_toml_lines()
    check(lines == ["[mcp]", "startup_timeout_ms = 120000",
                    "tool_timeout_ms = 900000"], "config.toml lines")


def test_generic_writer_merge():
    print("[offline] write_mcp_servers_json merge semantics")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "cfg.json")
        json.dump({"mcpServers": {"keep": {"command": "k"}}, "other": 1},
                  open(path, "w"))
        mcp.write_mcp_servers_json({"new": SPEC}, path, merge=True)
        doc = json.load(open(path))
        check("keep" in doc["mcpServers"] and "new" in doc["mcpServers"]
              and doc.get("other") == 1, "merge preserves entries + other keys")
        mcp.write_mcp_servers_json({"only": SPEC}, path, merge=False)
        doc = json.load(open(path))
        check(list(doc["mcpServers"]) == ["only"] and "other" not in doc,
              "merge=False replaces")


def test_pyagy_write_remove():
    print("[offline] pyagy write_mcp_servers / remove_mcp_servers round-trip")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "mcp_config.json")
        config.write_mcp_servers({"blender": SPEC}, path=path, merge=False)
        doc = json.load(open(path))
        entry = doc["mcpServers"]["blender"]
        check(entry["command"] == "/usr/bin/env"
              and entry["args"][:2] == ["-u", "PYTHONHOME"],
              "PYTHONHOME unwrap applied by default")
        config.write_mcp_servers({"plain": SPEC}, path=path,
                                 unset_pythonhome=False)
        doc = json.load(open(path))
        check(doc["mcpServers"]["plain"]["command"] == SPEC["command"]
              and "blender" in doc["mcpServers"], "merge + opt-out of wrap")
        removed = config.remove_mcp_servers(["blender", "absent"], path=path)
        doc = json.load(open(path))
        check(removed == 1 and "blender" not in doc["mcpServers"]
              and "plain" in doc["mcpServers"], "targeted removal")


def test_scoped_home():
    print("[offline] prepare_scoped_home(link_global_config=False) + onboarding")
    with tempfile.TemporaryDirectory() as td:
        root = conversations.prepare_scoped_home(td, link_global_config=False)
        cfg_dir = os.path.join(td, ".gemini", "config")
        check(os.path.isdir(cfg_dir) and not os.path.islink(cfg_dir),
              "real config dir (no global symlink)")
        marker = os.path.join(root, "cache", "onboarding.json")
        check(os.path.isfile(marker), "onboarding cache seeded")
        legacy = json.dumps({"consumerOnboardingComplete": True,
                             "enterpriseOnboardingComplete": False,
                             "onboardingComplete": True}, indent=2)
        check(open(marker).read() == legacy, "onboarding bytes identical")
        # the launch-time default call must leave the real dir alone
        conversations.prepare_scoped_home(td)
        check(os.path.isdir(cfg_dir) and not os.path.islink(cfg_dir),
              "later default call is a no-op on the real dir")


def main():
    test_normalize()
    test_env_wrapped()
    test_codex_flags()
    test_claude_args()
    test_qwen_fragment()
    test_kimi()
    test_generic_writer_merge()
    test_pyagy_write_remove()
    test_scoped_home()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
