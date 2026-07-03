#!/usr/bin/env python3
"""Tests for MCP config-injection (pyagy/config.py + agy_mcp_server.py spec loading).

Offline (always run, no agy): the built-in stub still serves; a rendered spec drives
the server (initialize/tools/list/resources/list handshake via validate_server);
static-``response`` and ``module:func`` handler tools both call; write_mcp_config
writes/merges ~/.gemini-style mcp_config.json preserving other servers; a <locals>
callable is rejected with a clear error.

    python3 test_scripts/test_config.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANTIGRAVITY = os.path.join(os.path.dirname(_HERE), "antigravity")
sys.path.insert(0, _ANTIGRAVITY)
# spawned MCP servers must import this module (for the module:func handler test)
os.environ["PYTHONPATH"] = os.pathsep.join(
    p for p in (_ANTIGRAVITY, _HERE, os.environ.get("PYTHONPATH", "")) if p)

from pyagy import config  # noqa: E402
from _mcp_handler import echo_upper  # noqa: E402  (importable handler, not __main__)

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_stub_server():
    print("[offline] built-in stub server (no spec)")
    os.environ.pop("AGY_MCP_SPEC", None)
    info = config.validate_server()   # no spec → stub
    check("tasksolver_query" in info["tools"], "stub: tasksolver_query advertised")
    check("agy://context/notes" in info["resources"], "stub: context resource advertised")


def test_spec_server():
    print("[offline] spec-driven server: static + handler tools, context")
    tools = [
        {"name": "static_tool", "description": "returns canned text",
         "input_schema": {"type": "object"}, "response": "canned-answer"},
        {"name": "handler_tool", "description": "runs a python handler",
         "input_schema": {"type": "object"}, "handler": echo_upper},
    ]
    context = [{"uri": "agy://ctx/readme", "text": "hello context", "mime_type": "text/plain"}]
    fd, spec = tempfile.mkstemp(suffix=".mcpspec.json")
    os.close(fd)
    try:
        config.write_spec(config.render_spec(tools, context), spec)
        doc = json.load(open(spec))
        # the callable was resolved to a module:func string in the spec
        h = next(t for t in doc["tools"] if t["name"] == "handler_tool")
        check(h.get("handler") == "_mcp_handler:echo_upper", "spec: callable → module:func string")

        info = config.validate_server(spec_path=spec)
        check(set(info["tools"]) == {"static_tool", "handler_tool"}, "spec: both tools advertised")
        check(info["resources"] == ["agy://ctx/readme"], "spec: context resource advertised")

        # tools/call for both, driving the server directly
        calls = _rpc_calls(spec, [
            (10, "tools/call", {"name": "static_tool", "arguments": {}}),
            (11, "tools/call", {"name": "handler_tool", "arguments": {"q": "hi"}}),
            (12, "resources/read", {"uri": "agy://ctx/readme"}),
        ])
        check(_text(calls[10]) == "canned-answer", "call: static tool returns canned text")
        check(_text(calls[11]) == "HANDLED:HI", "call: handler tool runs the callable")
        check(calls[12]["result"]["contents"][0]["text"] == "hello context",
              "read: context resource text")
    finally:
        os.remove(spec)


def test_write_and_merge():
    print("[offline] write_mcp_config writes + merges, preserving other servers")
    d = tempfile.mkdtemp(prefix="mcpcfg_")
    cfg = os.path.join(d, "mcp_config.json")
    with open(cfg, "w") as f:
        json.dump({"mcpServers": {"other": {"command": "x", "args": []}}}, f)
    tools = [{"name": "t1", "description": "", "response": "ok"}]
    path, spec = config.write_mcp_config(tools=tools, path=cfg, server_name="ts")
    doc = json.load(open(path))
    check("other" in doc["mcpServers"], "merge: pre-existing server preserved")
    check("ts" in doc["mcpServers"], "merge: new server added")
    ts = doc["mcpServers"]["ts"]
    check(ts["args"][0].endswith("pyagy/agy_mcp_server.py"), "merge: points at pyagy server")
    check(ts["env"]["AGY_MCP_SPEC"] == spec and os.path.exists(spec), "merge: spec written + wired")
    check(_ANTIGRAVITY in ts["env"]["PYTHONPATH"], "merge: PYTHONPATH includes antigravity")
    check(config.remove_mcp_config("ts", path) and
          "ts" not in json.load(open(path))["mcpServers"] and
          "other" in json.load(open(path))["mcpServers"], "remove: only target dropped")


def test_locals_rejected():
    print("[offline] non-importable (<locals>) handler is rejected")
    def local_handler(args):   # noqa: nested → not importable
        return "x"
    try:
        config.render_spec(tools=[{"name": "bad", "handler": local_handler}])
        check(False, "reject: raised for <locals> callable")
    except ValueError as e:
        check("importable" in str(e), "reject: clear error for <locals> callable")


# --- helpers: drive the server over stdio directly ---------------------------
def _rpc_calls(spec_path, requests):
    import subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (_ANTIGRAVITY, _HERE, env.get("PYTHONPATH", "")) if p)
    env["AGY_MCP_SPEC"] = spec_path
    p = subprocess.Popen([sys.executable, config.SERVER_SCRIPT], env=env, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    out = {}
    try:
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
        p.stdin.flush()
        p.stdout.readline()
        for mid, method, params in requests:
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                      "method": method, "params": params}) + "\n")
            p.stdin.flush()
            out[mid] = json.loads(p.stdout.readline())
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(timeout=5)
    return out


def _text(resp):
    return resp["result"]["content"][0]["text"]


def main():
    test_stub_server()
    test_spec_server()
    test_write_and_merge()
    test_locals_rejected()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
