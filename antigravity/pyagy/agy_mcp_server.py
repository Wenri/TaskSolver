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
import importlib
import json
import os
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "antigravity-tasksolver", "version": "0.1.0"}


def log(*a):
    print("[agy_mcp]", *a, file=sys.stderr, flush=True)


# ---- built-in stub (used when no AGY_MCP_SPEC is provided) -----------------
_STUB_TOOLS = [
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


def tool_tasksolver_query(args):
    prompt = args.get("prompt", "")
    model = args.get("model", "claude-code")
    # ---- plug TaskSolver in here -------------------------------------------
    # from tasksolver.agent import Agent
    # from tasksolver.common import TaskSpec, Question, ParsedAnswer  # define your task
    # agent = Agent(api_key, task, vision_model=model)
    # parsed, raw, meta, payload = agent.visual_interface.run_once(Question([prompt]))
    # return str(parsed)
    return f"[stub] TaskSolver({model}) would answer: {prompt!r}"


_STUB_IMPL = {"tasksolver_query": tool_tasksolver_query}
_STUB_RESOURCES = [
    {"uri": "agy://context/notes", "name": "Custom context notes",
     "mimeType": "text/plain"},
]
_STUB_RESOURCE_TEXT = {
    "agy://context/notes": "Custom context injected into agy via MCP. Replace with your own.",
}


def _resolve_handler(spec):
    """Turn a ``module:func`` (or ``module.func``) dotted string into a callable."""
    mod, _, name = spec.partition(":")
    if not name:
        mod, _, name = spec.rpartition(".")
    return getattr(importlib.import_module(mod), name)


def _load_spec(path):
    """Build (TOOLS, TOOL_IMPL, RESOURCES, RESOURCE_TEXT) from an AGY_MCP_SPEC file."""
    with open(path) as f:
        spec = json.load(f)
    tools, impl = [], {}
    for t in spec.get("tools", []) or []:
        name = t["name"]
        tools.append({"name": name, "description": t.get("description", ""),
                      "inputSchema": t.get("inputSchema") or {"type": "object"}})
        if "handler" in t:
            fn = _resolve_handler(t["handler"])
            impl[name] = lambda args, fn=fn: str(fn(args))
        else:
            text = t.get("response", "")
            impl[name] = lambda args, text=text: text
    resources, rtext = [], {}
    for r in spec.get("resources", []) or []:
        uri = r["uri"]
        resources.append({"uri": uri, "name": r.get("name") or uri,
                          "mimeType": r.get("mimeType", "text/plain")})
        rtext[uri] = r.get("text", "")
    info = spec.get("server_info") or {}
    return tools, impl, resources, rtext, info


_spec_path = os.environ.get("AGY_MCP_SPEC")
if _spec_path:
    try:
        TOOLS, TOOL_IMPL, RESOURCES, _RESOURCE_TEXT, _info = _load_spec(_spec_path)
        if _info:
            SERVER_INFO = {**SERVER_INFO, **_info}
    except Exception as e:
        log(f"failed to load AGY_MCP_SPEC={_spec_path!r}: {e}; using stub")
        TOOLS, TOOL_IMPL, RESOURCES, _RESOURCE_TEXT = (
            _STUB_TOOLS, _STUB_IMPL, _STUB_RESOURCES, _STUB_RESOURCE_TEXT)
else:
    TOOLS, TOOL_IMPL, RESOURCES, _RESOURCE_TEXT = (
        _STUB_TOOLS, _STUB_IMPL, _STUB_RESOURCES, _STUB_RESOURCE_TEXT)


def resource_read(uri):
    if uri in _RESOURCE_TEXT:
        return _RESOURCE_TEXT[uri]
    raise KeyError(uri)


# ---- JSON-RPC plumbing ----------------------------------------------------
def handle(req):
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": SERVER_INFO}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        impl = TOOL_IMPL.get(name)
        if not impl:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        try:
            text = impl(params.get("arguments") or {})
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:  # tools must not crash the server
            return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
    if method == "resources/list":
        return {"resources": RESOURCES}
    if method == "resources/read":
        uri = params.get("uri")
        try:
            return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": resource_read(uri)}]}
        except KeyError:
            return {"contents": []}
    raise ValueError(f"method not found: {method}")


def main():
    log("started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if "id" not in req:  # notification (e.g. notifications/initialized)
            continue
        resp = {"jsonrpc": "2.0", "id": req["id"]}
        try:
            resp["result"] = handle(req)
        except Exception as e:
            resp["error"] = {"code": -32603, "message": str(e)}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
