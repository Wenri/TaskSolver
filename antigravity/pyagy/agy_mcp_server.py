#!/usr/bin/env python3
"""Minimal stdio MCP server — the *native* half of the hybrid tools/mcp_context
path. agy is an MCP client, so registering this server (see config/mcp.json)
adds custom tools and context WITHOUT patching the binary.

Implements just enough of MCP (newline-delimited JSON-RPC 2.0 over stdio):
initialize, tools/list, tools/call, resources/list, resources/read.

Stdlib-only so it runs anywhere. To back a tool with TaskSolver, import and call
it inside the tool handler (see `tool_tasksolver_query`).
"""
import json
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "antigravity-tasksolver", "version": "0.1.0"}


def log(*a):
    print("[agy_mcp]", *a, file=sys.stderr, flush=True)


# ---- custom tools ---------------------------------------------------------
TOOLS = [
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


TOOL_IMPL = {"tasksolver_query": tool_tasksolver_query}

# ---- custom mcp_context (resources) ---------------------------------------
RESOURCES = [
    {"uri": "agy://context/notes", "name": "Custom context notes",
     "mimeType": "text/plain"},
]


def resource_read(uri):
    if uri == "agy://context/notes":
        return "Custom context injected into agy via MCP. Replace with your own."
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
