#!/usr/bin/env python3
"""Tests for MCP config-injection (pyagy/config.py + agy_mcp_server.py spec loading).

Offline (always run, no agy): the built-in stub still serves; a rendered spec drives
the server (initialize/tools/list/resources/list handshake via validate_server);
static-``response`` and ``module:func`` handler tools both call; write_mcp_config
writes/merges ~/.gemini-style mcp_config.json preserving other servers; a <locals>
callable is rejected with a clear error.

    python3 test_scripts/test_config.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO, cast
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)                       # repo root holds the shared `wirecap` package
_ANTIGRAVITY = os.path.join(_REPO, "antigravity")
sys.path.insert(0, _ANTIGRAVITY)
sys.path.insert(0, _REPO)
# spawned MCP servers must import this module (for the module:func handler test)
os.environ["PYTHONPATH"] = os.pathsep.join(
    p for p in (_ANTIGRAVITY, _HERE, os.environ.get("PYTHONPATH", "")) if p)


from _mcp_handler import echo_upper  # noqa: E402  (importable handler, not __main__)
from pyagy import config  # noqa: E402
from pyagy.types import ContextInput, ToolInput

from wirecap.types import (
    JSONObject,
    json_array,
    json_load_object,
    json_loads_object,
    json_object,
)

_failures: list[str] = []


def check(cond: object, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_stub_server() -> None:
    print("[offline] built-in stub server (no spec)")
    os.environ.pop("AGY_MCP_SPEC", None)
    info = config.validate_server()   # no spec → stub
    check("tasksolver_query" in info["tools"], "stub: tasksolver_query advertised")
    check("agy://context/notes" in info["resources"], "stub: context resource advertised")


def test_spec_server() -> None:
    print("[offline] spec-driven server: static + handler tools, context")
    tools: list[ToolInput] = [
        {"name": "static_tool", "description": "returns canned text",
         "input_schema": {"type": "object"}, "response": "canned-answer"},
        {"name": "handler_tool", "description": "runs a python handler",
         "input_schema": {"type": "object"}, "handler": echo_upper},
    ]
    context: list[ContextInput] = [{"uri": "agy://ctx/readme", "text": "hello context", "mime_type": "text/plain"}]
    fd, spec = tempfile.mkstemp(suffix=".mcpspec.json")
    os.close(fd)
    try:
        config.write_spec(config.render_spec(tools, context), spec)
        doc = json_load_object(open(spec))
        # the callable was resolved to a module:func string in the spec
        h = next(tool for item in json_array(doc["tools"])
                 if (tool := json_object(item))["name"] == "handler_tool")
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
        resource = json_object(json_array(json_object(calls[12]["result"])["contents"])[0])
        check(resource["text"] == "hello context",
              "read: context resource text")
    finally:
        os.remove(spec)


def test_write_and_merge() -> None:
    print("[offline] write_mcp_config writes + merges, preserving other servers")
    d = tempfile.mkdtemp(prefix="mcpcfg_")
    cfg = os.path.join(d, "mcp_config.json")
    with open(cfg, "w") as f:
        json.dump({"mcpServers": {"other": {"command": "x", "args": []}}}, f)
    tools: list[ToolInput] = [{"name": "t1", "description": "", "response": "ok"}]
    path, spec = config.write_mcp_config(tools=tools, path=cfg, server_name="ts")
    doc = json_load_object(open(path))
    check("other" in json_object(doc["mcpServers"]), "merge: pre-existing server preserved")
    check("ts" in json_object(doc["mcpServers"]), "merge: new server added")
    ts = json_object(json_object(doc["mcpServers"])["ts"])
    server_script = json_array(ts["args"])[0]
    server_env = json_object(ts["env"])
    python_path = server_env["PYTHONPATH"]
    assert isinstance(server_script, str) and isinstance(python_path, str)
    check(server_script.endswith("pyagy/agy_mcp_server.py"), "merge: points at pyagy server")
    check(server_env["AGY_MCP_SPEC"] == spec and os.path.exists(spec), "merge: spec written + wired")
    check(_ANTIGRAVITY in python_path, "merge: PYTHONPATH includes antigravity")
    check(config.remove_mcp_config("ts", path) and
          "ts" not in json_object(json_load_object(open(path))["mcpServers"]) and
          "other" in json_object(json_load_object(open(path))["mcpServers"]), "remove: only target dropped")


def test_locals_rejected() -> None:
    print("[offline] non-importable (<locals>) handler is rejected")
    def local_handler(args: JSONObject) -> str:   # Nested handlers cannot be imported by the child.
        return "x"
    try:
        config.render_spec(tools=[{"name": "bad", "handler": local_handler}])
        check(False, "reject: raised for <locals> callable")
    except ValueError as e:
        check("importable" in str(e), "reject: clear error for <locals> callable")


def test_validation_deadline_and_cleanup() -> None:
    print("[offline] validation deadline covers stalls, partial lines and the complete handshake")
    delayed = (
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    time.sleep(0.2)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {}}), flush=True)\n"
    )
    cases = {
        "stall": "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n",
        "partial line": "sys.stdout.write('{\"jsonrpc\":'); sys.stdout.flush(); time.sleep(60)\n",
        "total handshake": delayed,
    }
    for label, body in cases.items():
        with tempfile.TemporaryDirectory() as directory:
            server = Path(directory) / "server.py"
            pid_path, spec_path = Path(directory) / "pid", Path(directory) / "spec"
            server.write_text(
                "import json, os, signal, sys, time\nfrom pathlib import Path\n"
                f"Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
                f"Path({str(spec_path)!r}).write_text(os.environ['AGY_MCP_SPEC'])\n" + body)
            started = time.monotonic()
            with patch.object(config, "SERVER_SCRIPT", str(server)):
                try:
                    config.validate_server(tools=[{"name": "t", "response": "ok"}], timeout=0.3)
                except TimeoutError:
                    check(True, f"validation: {label} times out")
                else:
                    check(False, f"validation: {label} times out")
            check(time.monotonic() - started < 1.5, f"validation: {label} respects bounded cleanup")
            pid = int(pid_path.read_text())
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                check(True, f"validation: {label} child reaped")
            else:
                check(False, f"validation: {label} child reaped")
            check(not Path(spec_path.read_text()).exists(), f"validation: {label} temporary spec removed")


def test_validation_noisy_server_and_invalid_deadlines() -> None:
    print("[offline] a noisy valid server completes; invalid deadlines do not spawn")
    with tempfile.TemporaryDirectory() as directory:
        server = Path(directory) / "server.py"
        pid_path = Path(directory) / "pid"
        server.write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            f"Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
            "sys.stderr.write('noise' * 262144); sys.stderr.flush()\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    result = {'tools': [{'name': 'valid'}], 'resources': [{'uri': 'test://valid'}]}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)\n")
        with patch.object(config, "SERVER_SCRIPT", str(server)):
            check(config.validate_server(timeout=2) == {"tools": ["valid"], "resources": ["test://valid"]},
                  "validation: stderr cannot block a normal handshake")
        try:
            os.waitpid(int(pid_path.read_text()), os.WNOHANG)
        except ChildProcessError:
            check(True, "validation: normal server child reaped")
        else:
            check(False, "validation: normal server child reaped")
    for timeout in (0, -1, float("nan"), float("inf")):
        with patch.object(config.subprocess, "Popen") as spawn:
            try:
                config.validate_server(timeout=timeout)
            except ValueError:
                check(True, f"validation: rejects timeout {timeout}")
            else:
                check(False, f"validation: rejects timeout {timeout}")
            spawn.assert_not_called()


# --- helpers: drive the server over stdio directly ---------------------------
def _rpc_calls(spec_path: str, requests: Sequence[tuple[int, str, JSONObject]]) -> dict[int, JSONObject]:
    import subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (_ANTIGRAVITY, _HERE, env.get("PYTHONPATH", "")) if p)
    env["AGY_MCP_SPEC"] = spec_path
    p = subprocess.Popen([sys.executable, config.SERVER_SCRIPT], env=env, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    out: dict[int, JSONObject] = {}
    assert p.stdin is not None and p.stdout is not None
    stdin, stdout = cast(TextIO, p.stdin), cast(TextIO, p.stdout)
    try:
        stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
        stdin.flush()
        stdout.readline()
        for mid, method, params in requests:
            stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                      "method": method, "params": params}) + "\n")
            stdin.flush()
            out[mid] = json_loads_object(stdout.readline())
    finally:
        stdin.close()
        p.terminate()
        p.wait(timeout=5)
    return out


def _text(resp: JSONObject) -> str:
    content = json_array(json_object(resp["result"])["content"])
    text = json_object(content[0])["text"]
    assert isinstance(text, str)
    return text


def main() -> None:
    test_stub_server()
    test_spec_server()
    test_write_and_merge()
    test_locals_rejected()
    test_validation_deadline_and_cleanup()
    test_validation_noisy_server_and_invalid_deadlines()
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
