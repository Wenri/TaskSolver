#!/usr/bin/env python3
"""Minimal stdio MCP server — the *native* half of the hybrid tools/mcp_context
path. agy is an MCP client, so registering this server (see config/mcp.json)
adds custom tools and context WITHOUT patching the binary.

Implements just enough of MCP (newline-delimited JSON-RPC 2.0 over stdio):
initialize, tools/list, tools/call, resources/list, resources/read.

Stdlib-only so it runs anywhere. Two ways to define what it serves:
  * AGY_MCP_SPEC=<path> — a JSON spec (written by pyagy.config.write_mcp_config)
    with ``tools`` (each backed by a static ``response`` or a ``handler`` =
    ``module:func`` dotted path this server imports) and ``resources`` (context,
    each with inline ``text``).
  * unset — the built-in ``tasksolver_query`` stub below (a template to edit).
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from wirecap.types import JSONObject, JSONValue

type ToolHandler = Callable[["JSONObject"], object]


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    return value


def _array(value: JSONValue) -> list[JSONValue]:
    if not isinstance(value, list):
        raise TypeError("expected a JSON array")
    return value


def _string(value: JSONValue) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a string")
    return value

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO: JSONObject = {"name": "antigravity-tasksolver", "version": "0.1.0"}


def log(*a: object) -> None:
    print("[agy_mcp]", *a, file=sys.stderr, flush=True)


# ---- built-in stub (used when no AGY_MCP_SPEC is provided) -----------------
_STUB_TOOLS: list[JSONValue] = [
    {
        "name": "tasksolver_query",
        "description": "Route a prompt through TaskSolver (provider-agnostic VLM query flow).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The question/prompt."},
                "model": {"type": "string", "description": "Model id (e.g. claude-code, gpt, gemini).",
                          "default": "claude-code"},
            },
            "required": ["prompt"],
        },
    },
]


def tool_tasksolver_query(args: JSONObject) -> str:
    prompt = args.get("prompt", "")
    model = args.get("model", "claude-code")
    # ---- plug TaskSolver in here -------------------------------------------
    # from tasksolver.agent import Agent
    # from tasksolver.common import TaskSpec, Question, ParsedAnswer  # define your task
    # agent = Agent(api_key, task, vision_model=model)
    # parsed, raw, meta, payload = agent.visual_interface.run_once(Question([prompt]))
    # return str(parsed)
    return f"[stub] TaskSolver({model}) would answer: {prompt!r}"


_STUB_IMPL: dict[str, ToolHandler] = {"tasksolver_query": tool_tasksolver_query}
_STUB_RESOURCES: list[JSONValue] = [
    {"uri": "agy://context/notes", "name": "Custom context notes",
     "mimeType": "text/plain"},
]
_STUB_RESOURCE_TEXT = {
    "agy://context/notes": "Custom context injected into agy via MCP. Replace with your own.",
}


def _resolve_handler(spec: str) -> ToolHandler:
    """Turn a ``module:func`` (or ``module.func``) dotted string into a callable."""
    mod, _, name = spec.partition(":")
    if not name:
        mod, _, name = spec.rpartition(".")
    handler = cast(object, getattr(importlib.import_module(mod), name))
    if not callable(handler):
        raise TypeError(f"tool handler {spec!r} is not callable")
    return cast(ToolHandler, handler)


def _load_spec(path: str) -> tuple[list[JSONValue], dict[str, ToolHandler], list[JSONValue], dict[str, str], JSONObject]:
    """Build (TOOLS, TOOL_IMPL, RESOURCES, RESOURCE_TEXT) from an AGY_MCP_SPEC file."""
    with open(path) as f:
        spec = _object(cast("JSONValue", json.load(f)))
    tools: list[JSONValue] = []
    impl: dict[str, ToolHandler] = {}
    for value in _array(spec.get("tools") or []):
        t = _object(value)
        name = _string(t["name"])
        tools.append({"name": name, "description": t.get("description", ""),
                      "inputSchema": t.get("inputSchema") or {"type": "object"}})
        if "handler" in t:
            fn = _resolve_handler(_string(t["handler"]))
            impl[name] = fn
        else:
            text = _string(t.get("response", ""))

            def static_response(_args: JSONObject, text: str = text) -> str:
                return text

            impl[name] = static_response
    resources: list[JSONValue] = []
    rtext: dict[str, str] = {}
    for value in _array(spec.get("resources") or []):
        r = _object(value)
        uri = _string(r["uri"])
        resources.append({"uri": uri, "name": r.get("name") or uri,
                          "mimeType": r.get("mimeType", "text/plain")})
        rtext[uri] = _string(r.get("text", ""))
    info = _object(spec.get("server_info") or {})
    return tools, impl, resources, rtext, info


def _configuration() -> tuple[list[JSONValue], dict[str, ToolHandler], list[JSONValue], dict[str, str]]:
    path = os.environ.get("AGY_MCP_SPEC")
    if path:
        try:
            tools, implementations, resources, text, info = _load_spec(path)
            SERVER_INFO.update(info)
            return tools, implementations, resources, text
        except Exception as error:
            log(f"failed to load AGY_MCP_SPEC={path!r}: {error}; using stub")
    return _STUB_TOOLS, _STUB_IMPL, _STUB_RESOURCES, _STUB_RESOURCE_TEXT


TOOLS, TOOL_IMPL, RESOURCES, _RESOURCE_TEXT = _configuration()


def resource_read(uri: str) -> str:
    if uri in _RESOURCE_TEXT:
        return _RESOURCE_TEXT[uri]
    raise KeyError(uri)


# ---- JSON-RPC plumbing ----------------------------------------------------
def handle(req: JSONObject) -> JSONObject:
    method = req.get("method")
    params = _object(req.get("params") or {})
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": SERVER_INFO}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = _string(params.get("name"))
        impl = TOOL_IMPL.get(name)
        if not impl:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        try:
            text = str(impl(_object(params.get("arguments") or {})))
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:  # tools must not crash the server
            return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
    if method == "resources/list":
        return {"resources": RESOURCES}
    if method == "resources/read":
        uri = _string(params.get("uri"))
        try:
            return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": resource_read(uri)}]}
        except KeyError:
            return {"contents": []}
    raise ValueError(f"method not found: {method}")


def main() -> None:
    log("started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = _object(cast("JSONValue", json.loads(line)))
        except Exception:
            continue
        if "id" not in req:  # notification (e.g. notifications/initialized)
            continue
        resp: JSONObject = {"jsonrpc": "2.0", "id": req["id"]}
        try:
            resp["result"] = handle(req)
        except Exception as e:
            resp["error"] = {"code": -32603, "message": str(e)}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
