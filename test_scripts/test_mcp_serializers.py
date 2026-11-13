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

from __future__ import annotations

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
    import importlib
    importlib.import_module("pygit2")
except ImportError:
    import types
    sys.modules["pygit2"] = types.ModuleType("pygit2")

from pyagy import (
    config,  # noqa: E402
    conversations,  # noqa: E402
)

from wirecap.runtime import mcp  # noqa: E402
from wirecap.types import json_array, json_load_object, json_loads_object, json_object

_failures: list[str] = []


def check(cond: object, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


SPEC = {"command": "/usr/bin/uv",
        "args": ["run", "--directory", "/repo/BlenderMCP/official/mcp", "blender-mcp"],
        "env": {"BLENDER_MCP_HOST": "127.0.0.1", "BLENDER_MCP_PORT": "9999"}}


def test_normalize() -> None:
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


def test_env_wrapped() -> None:
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


def test_codex_flags() -> None:
    print("[offline] codex_config_flags")
    flags = mcp.codex_config_flags({"official-blender-mcp": SPEC})
    check(flags[0] == "-c" and flags[1].startswith("mcp_servers.official-blender-mcp="),
          "dotted key")
    rendered = flags[1].split("=", 1)[1]
    check('"/usr/bin/uv"' in rendered and 'BLENDER_MCP_PORT = "9999"' in rendered,
          "inline TOML table")
    # strings go through JSON escaping so quoted paths survive
    tricky: mcp.MCPServerSpec = {"command": 'a"b', "args": [], "env": {}}
    val = mcp.codex_config_flags({"s": tricky})[1]
    check('\\"' in val, "quotes escaped")
    try:
        mcp.codex_config_flags({"bad name": SPEC})
        check(False, "non-bare key rejected")
    except ValueError:
        check(True, "non-bare key rejected")


def test_claude_args() -> None:
    print("[offline] claude_mcp_args")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "mcp-servers.json")
        args = mcp.claude_mcp_args({"blender": SPEC}, path)
        check(args == ["--mcp-config", path, "--strict-mcp-config"], "flags")
        doc = json_load_object(open(path))
        check(doc == {"mcpServers": {"blender": mcp.normalize_server_spec(SPEC)}},
              "file content")
        # byte-parity with the historical harness writer
        legacy = json.dumps({"mcpServers": {"blender": SPEC}}, indent=2)
        check(open(path).read() == legacy, "byte-identical to json.dumps(indent=2)")
        args = mcp.claude_mcp_args({"blender": SPEC}, path, strict=False)
        check(args == ["--mcp-config", path], "strict off")


def test_qwen_fragment() -> None:
    print("[offline] qwen_mcp_servers")
    frag = mcp.qwen_mcp_servers({"blender": SPEC})
    check(frag["blender"].get("trust") is True and frag["blender"].get("timeout") == 60000,
          "trust+timeout added")
    check(frag["blender"]["command"] == SPEC["command"], "spec preserved")
    # byte-parity with the historical inline comprehension
    legacy = {name: {**spec, "trust": True, "timeout": 60000}
              for name, spec in {"blender": SPEC}.items()}
    check(json.dumps(frag, indent=2) == json.dumps(legacy, indent=2),
          "byte-identical fragment")


def test_kimi() -> None:
    print("[offline] kimi serializations")
    check(json_object(json_object(json_loads_object(mcp.kimi_mcp_json({"b": SPEC}))["mcpServers"])["b"])["command"]
          == SPEC["command"], "mcp.json shape")
    lines = mcp.kimi_mcp_toml_lines()
    check(lines == ["[mcp]", "startup_timeout_ms = 120000",
                    "tool_timeout_ms = 900000"], "config.toml lines")


def test_generic_writer_merge() -> None:
    print("[offline] write_mcp_servers_json merge semantics")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "cfg.json")
        json.dump({"mcpServers": {"keep": {"command": "k"}}, "other": 1},
                  open(path, "w"))
        mcp.write_mcp_servers_json({"new": SPEC}, path, merge=True)
        doc = json_load_object(open(path))
        check("keep" in json_object(doc["mcpServers"]) and "new" in json_object(doc["mcpServers"])
              and doc.get("other") == 1, "merge preserves entries + other keys")
        mcp.write_mcp_servers_json({"only": SPEC}, path, merge=False)
        doc = json_load_object(open(path))
        check(list(json_object(doc["mcpServers"])) == ["only"] and "other" not in doc,
              "merge=False replaces")


def test_pyagy_write_remove() -> None:
    print("[offline] pyagy write_mcp_servers / remove_mcp_servers round-trip")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "mcp_config.json")
        config.write_mcp_servers({"blender": SPEC}, path=path, merge=False)
        doc = json_load_object(open(path))
        entry = json_object(json_object(doc["mcpServers"])["blender"])
        check(entry["command"] == "/usr/bin/env"
              and json_array(entry["args"])[:2] == ["-u", "PYTHONHOME"],
              "PYTHONHOME unwrap applied by default")
        config.write_mcp_servers({"plain": SPEC}, path=path,
                                 unset_pythonhome=False)
        doc = json_load_object(open(path))
        check(json_object(json_object(doc["mcpServers"])["plain"])["command"] == SPEC["command"]
              and "blender" in json_object(doc["mcpServers"]), "merge + opt-out of wrap")
        removed = config.remove_mcp_servers(["blender", "absent"], path=path)
        doc = json_load_object(open(path))
        check(removed == 1 and "blender" not in json_object(doc["mcpServers"])
              and "plain" in json_object(doc["mcpServers"]), "targeted removal")


def test_scoped_home() -> None:
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


def test_app_data_dir_isolation() -> None:
    print("[offline] dedicated ~/.gemini/pyagy history + shared global auth/settings")
    with tempfile.TemporaryDirectory() as fake_home:
        old_home = os.environ.get("HOME")
        old_env_dir = os.environ.pop(conversations.APP_DATA_DIR_ENV, None)
        os.environ["HOME"] = fake_home
        try:
            global_app = os.path.join(fake_home, ".gemini", "antigravity-cli")
            os.makedirs(os.path.join(global_app, "cache"), exist_ok=True)
            with open(os.path.join(global_app, "antigravity-oauth-token"), "w") as f:
                f.write("tok")
            with open(os.path.join(global_app, "installation_id"), "w") as f:
                f.write("inst-1")
            with open(os.path.join(global_app, "cache", "default_project_id.txt"), "w") as f:
                f.write("proj-1")
            global_settings = {
                "allowNonWorkspaceAccess": True,
                "colorScheme": "dark",
                "model": "gemini-3.8-flash-low",
                "toolPermission": "always-proceed",
                "useG1Credits": True,
                "trustedWorkspaces": ["/interactive/only"],
            }
            with open(os.path.join(global_app, "settings.json"), "w") as f:
                json.dump(global_settings, f, indent=2)

            check(conversations.store_root() == os.path.join(fake_home, ".gemini", "pyagy"),
                  "global default store_root is ~/.gemini/pyagy")
            scoped_td = os.path.join(fake_home, "scoped")
            check(conversations.store_root(home=scoped_td)
                  == os.path.join(scoped_td, ".gemini", "antigravity-cli"),
                  "scoped home default store_root is <home>/.gemini/antigravity-cli")

            pyagy_root = conversations.prepare_app_data_dir()
            check(os.path.islink(os.path.join(pyagy_root, "antigravity-oauth-token")),
                  "oauth token symlinked")
            check(os.path.islink(os.path.join(pyagy_root, "installation_id")),
                  "installation_id symlinked")
            check(os.path.islink(os.path.join(pyagy_root, "cache", "default_project_id.txt")),
                  "default_project_id.txt symlinked")
            check(os.path.isfile(os.path.join(pyagy_root, "cache", "onboarding.json")),
                  "onboarding seeded in pyagy store")

            ws = os.path.join(fake_home, "throwaway-ws")
            check(conversations.trust_workspace(ws), "trust_workspace writes to pyagy store")
            pyagy_doc = json_load_object(open(os.path.join(pyagy_root, "settings.json")))
            global_doc = json_load_object(open(os.path.join(global_app, "settings.json")))
            check(pyagy_doc.get("allowNonWorkspaceAccess") is True
                  and pyagy_doc.get("useG1Credits") is True
                  and pyagy_doc.get("toolPermission") == "always-proceed"
                  and pyagy_doc.get("model") == "gemini-3.8-flash-low",
                  "global preferences synced into pyagy settings.json")
            check(pyagy_doc.get("trustedWorkspaces") == [ws],
                  "pyagy trustedWorkspaces has only local workspace")
            check(global_doc.get("trustedWorkspaces") == ["/interactive/only"],
                  "global trustedWorkspaces untouched")

            # Live re-sync picks up global model change while preserving pyagy trustedWorkspaces
            global_settings["model"] = "gemini-3.1-pro-low"
            with open(os.path.join(global_app, "settings.json"), "w") as f:
                json.dump(global_settings, f, indent=2)
            conversations.prepare_app_data_dir()
            pyagy_doc2 = json_load_object(open(os.path.join(pyagy_root, "settings.json")))
            check(pyagy_doc2.get("model") == "gemini-3.1-pro-low"
                  and pyagy_doc2.get("trustedWorkspaces") == [ws],
                  "re-sync updates global keys and preserves local trustedWorkspaces")

            for bad in ("/abs/path", "../escape", "."):
                try:
                    conversations.prepare_app_data_dir(bad)
                    check(False, f"invalid app_data_dir rejected: {bad}")
                except ValueError:
                    check(True, f"invalid app_data_dir rejected: {bad}")
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            if old_env_dir is not None:
                os.environ[conversations.APP_DATA_DIR_ENV] = old_env_dir


def main() -> None:
    test_normalize()
    test_env_wrapped()
    test_codex_flags()
    test_claude_args()
    test_qwen_fragment()
    test_kimi()
    test_generic_writer_merge()
    test_pyagy_write_remove()
    test_scoped_home()
    test_app_data_dir_isolation()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
