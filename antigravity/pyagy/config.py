"""Config-injection: register a TaskSolver-backed MCP server with agy.

The *additive* half of the modify story (the SYNC rewrite path can only substitute
equal-length bytes, so it can't add tools/context). agy is an MCP client, so writing
an ``mcpServers`` entry gives it new tools and context resources without patching the
binary. This module renders a spec from ``tools``/``context`` and points agy's config
at ``pyagy/agy_mcp_server.py`` (which loads that spec via ``AGY_MCP_SPEC``).

Config path is resolved for agy 1.0.16: ``~/.gemini/config/mcp_config.json`` (schema
``{"mcpServers": {name: {command, args, env}}}``). Override with
``AGY_MCP_CONFIG_PATH`` / the ``path`` arg for other versions;
``validate_server()`` runs the initialize/tools-list handshake before you spawn agy.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # antigravity/
SERVER_SCRIPT = os.path.join(ROOT, "pyagy", "agy_mcp_server.py")
DEFAULT_SERVER_NAME = "antigravity-tasksolver"


def default_config_path():
    return os.path.expanduser("~/.gemini/config/mcp_config.json")


def detect_config_path():
    """The mcp_config.json agy reads: ``AGY_MCP_CONFIG_PATH`` if set, else the 1.0.16
    default (``~/.gemini/config/mcp_config.json``)."""
    return os.environ.get("AGY_MCP_CONFIG_PATH") or default_config_path()


# --- normalize ToolSpec/ContextResource objects OR plain dicts ---------------
def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _tool_to_spec(t):
    """Accept a client.ToolSpec-like object or a dict → the on-disk tool entry.
    A tool is backed by a static ``response`` string or a ``handler`` = ``module:func``
    dotted path the *server* imports (a live callable can't cross the subprocess
    boundary, so callables are resolved to their ``module:qualname``)."""
    schema = _get(t, "input_schema") or _get(t, "inputSchema") or {"type": "object"}
    handler = _get(t, "handler")
    entry = {
        "name": _get(t, "name"),
        "description": _get(t, "description", ""),
        "inputSchema": schema,
    }
    if callable(handler):
        mod = getattr(handler, "__module__", None)
        qual = getattr(handler, "__qualname__", getattr(handler, "__name__", None))
        if not mod or mod == "__main__" or "<locals>" in (qual or ""):
            raise ValueError(
                f"tool {entry['name']!r} handler must be a top-level importable function "
                f"(got {mod}:{qual}); pass a 'module:func' string or a static 'response'")
        entry["handler"] = f"{mod}:{qual}"
    elif isinstance(handler, str):
        entry["handler"] = handler
    else:
        entry["response"] = _get(t, "response", "")
    return entry


def _context_to_spec(c):
    return {
        "uri": _get(c, "uri"),
        "name": _get(c, "name") or _get(c, "uri"),
        "mimeType": _get(c, "mime_type") or _get(c, "mimeType") or "text/plain",
        "text": _get(c, "text", ""),
    }


def render_spec(tools=None, context=None, server_info=None):
    return {
        "server_info": server_info or {},
        "tools": [_tool_to_spec(t) for t in (tools or [])],
        "resources": [_context_to_spec(c) for c in (context or [])],
    }


def write_spec(spec, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(spec, f, indent=2)
    return path


def write_mcp_config(tools=None, context=None, *, server_name=DEFAULT_SERVER_NAME,
                     path=None, spec_path=None, python=None, pythonpath=None,
                     server_info=None, merge=True):
    """Render ``tools``/``context`` to a spec file and register (or update) an
    ``mcpServers`` entry that runs ``agy_mcp_server.py`` against it. Existing servers
    are preserved when ``merge`` is true. Returns ``(config_path, spec_path)``."""
    path = path or detect_config_path()
    spec_path = spec_path or os.path.join(os.path.dirname(path), f"{server_name}.mcpspec.json")
    write_spec(render_spec(tools, context, server_info), spec_path)

    pp = os.pathsep.join(p for p in (pythonpath, ROOT) if p)
    entry = {
        "command": python or sys.executable or "python3",
        "args": [SERVER_SCRIPT],
        "env": {"PYTHONPATH": pp, "AGY_MCP_SPEC": spec_path},
    }
    doc = {}
    if merge and os.path.exists(path):
        try:
            with open(path) as f:
                txt = f.read().strip()
            doc = json.loads(txt) if txt else {}
        except (ValueError, OSError):
            doc = {}
    servers = doc.setdefault("mcpServers", {})
    servers[server_name] = entry
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    return path, spec_path


def remove_mcp_config(server_name=DEFAULT_SERVER_NAME, path=None):
    """Remove a previously-registered server entry (leaves other servers intact)."""
    path = path or detect_config_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            txt = f.read().strip()
        doc = json.loads(txt) if txt else {}
    except (ValueError, OSError):
        return False
    if server_name in doc.get("mcpServers", {}):
        del doc["mcpServers"][server_name]
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
        return True
    return False


def validate_server(spec_path=None, tools=None, context=None, timeout=15):
    """Spawn ``agy_mcp_server.py`` and run the MCP handshake (initialize, tools/list,
    resources/list) directly, without agy — a fast pre-flight that the server starts
    and advertises the expected tools. Returns ``{"tools": [...], "resources": [...]}``.
    Provide either an existing ``spec_path`` or ``tools``/``context`` (rendered to a
    temp spec). Raises on protocol error."""
    import tempfile
    tmp = None
    if spec_path is None and (tools or context):
        fd, tmp = tempfile.mkstemp(suffix=".mcpspec.json")
        os.close(fd)
        write_spec(render_spec(tools, context), tmp)
        spec_path = tmp
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (ROOT, env.get("PYTHONPATH", "")) if p)
    if spec_path:
        env["AGY_MCP_SPEC"] = spec_path
    proc = subprocess.Popen(
        [sys.executable, SERVER_SCRIPT], env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        def call(mid, method, params=None):
            proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": mid, "method": method,
                 "params": params or {}}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed the stream during handshake")
            return json.loads(line)

        init = call(1, "initialize")
        if "result" not in init:
            raise RuntimeError(f"initialize failed: {init}")
        tlist = call(2, "tools/list").get("result", {}).get("tools", [])
        rlist = call(3, "resources/list").get("result", {}).get("resources", [])
        return {"tools": [t.get("name") for t in tlist],
                "resources": [r.get("uri") for r in rlist]}
    finally:
        try:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        if tmp:
            os.remove(tmp)
