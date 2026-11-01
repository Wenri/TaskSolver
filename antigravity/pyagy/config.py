"""Config-injection: register a TaskSolver-backed MCP server with agy.

agy is an MCP client, so writing
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
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

from wirecap.types import JSONObject, json_array, json_loads_object, json_object
from .types import ContextInput, ContextResource, ToolInput, ToolSpec

if TYPE_CHECKING:
    from wirecap.runtime.mcp import MCPServerSpec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # antigravity/
SERVER_SCRIPT = os.path.join(ROOT, "pyagy", "agy_mcp_server.py")
DEFAULT_SERVER_NAME = "antigravity-tasksolver"


def default_config_path() -> str:
    return os.path.expanduser("~/.gemini/config/mcp_config.json")


def detect_config_path() -> str:
    """The mcp_config.json agy reads: ``AGY_MCP_CONFIG_PATH`` if set, else the 1.0.16
    default (``~/.gemini/config/mcp_config.json``)."""
    return os.environ.get("AGY_MCP_CONFIG_PATH") or default_config_path()


def _tool_to_spec(t: ToolSpec | ToolInput) -> JSONObject:
    """Accept a client.ToolSpec-like object or a dict → the on-disk tool entry.
    A tool is backed by a static ``response`` string or a ``handler`` = ``module:func``
    dotted path the *server* imports (a live callable can't cross the subprocess
    boundary, so callables are resolved to their ``module:qualname``)."""
    if isinstance(t, ToolSpec):
        name, description, schema, handler, response = (
            t.name, t.description, t.input_schema, t.handler, t.response)
    else:
        name, description = t["name"], t.get("description", "")
        schema = t.get("input_schema") or t.get("inputSchema")
        handler, response = t.get("handler"), t.get("response", "")
    entry: JSONObject = {
        "name": name,
        "description": description,
        "inputSchema": schema or {"type": "object"},
    }
    if callable(handler):
        mod = cast(object, getattr(handler, "__module__", None))
        qual = cast(object, getattr(handler, "__qualname__", getattr(handler, "__name__", None)))
        if (not isinstance(mod, str) or not isinstance(qual, str) or not mod or not qual
                or mod == "__main__" or "<locals>" in qual):
            raise ValueError(
                f"tool {entry['name']!r} handler must be a top-level importable function "
                f"(got {mod}:{qual}); pass a 'module:func' string or a static 'response'")
        entry["handler"] = f"{mod}:{qual}"
    elif isinstance(handler, str):
        entry["handler"] = handler
    else:
        entry["response"] = response
    return entry


def _context_to_spec(c: ContextResource | ContextInput) -> JSONObject:
    if isinstance(c, ContextResource):
        uri, name, mime, text = c.uri, c.name, c.mime_type, c.text
    else:
        uri, name = c["uri"], c.get("name")
        mime, text = c.get("mime_type") or c.get("mimeType"), c.get("text", "")
    return {
        "uri": uri,
        "name": name or uri,
        "mimeType": mime or "text/plain",
        "text": text,
    }


def render_spec(tools: Sequence[ToolSpec | ToolInput] | None = None,
                context: Sequence[ContextResource | ContextInput] | None = None,
                server_info: JSONObject | None = None) -> JSONObject:
    return {
        "server_info": server_info or {},
        "tools": [_tool_to_spec(t) for t in (tools or [])],
        "resources": [_context_to_spec(c) for c in (context or [])],
    }


def write_spec(spec: JSONObject, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(spec, f, indent=2)
    return path


def write_mcp_config(tools: Sequence[ToolSpec | ToolInput] | None = None,
                     context: Sequence[ContextResource | ContextInput] | None = None, *,
                     server_name: str = DEFAULT_SERVER_NAME, path: str | None = None,
                     spec_path: str | None = None, python: str | None = None,
                     pythonpath: str | None = None, server_info: JSONObject | None = None,
                     merge: bool = True) -> tuple[str, str]:
    """Render ``tools``/``context`` to a spec file and register (or update) an
    ``mcpServers`` entry that runs ``agy_mcp_server.py`` against it. Existing servers
    are preserved when ``merge`` is true. Returns ``(config_path, spec_path)``."""
    path = path or detect_config_path()
    spec_path = spec_path or os.path.join(os.path.dirname(path), f"{server_name}.mcpspec.json")
    write_spec(render_spec(tools, context, server_info), spec_path)

    pp = os.pathsep.join(p for p in (pythonpath, ROOT) if p)
    entry: JSONObject = {
        "command": python or sys.executable or "python3",
        "args": [SERVER_SCRIPT],
        "env": {"PYTHONPATH": pp, "AGY_MCP_SPEC": spec_path},
    }
    doc: JSONObject = {}
    if merge and os.path.exists(path):
        try:
            with open(path) as f:
                txt = f.read().strip()
            doc = json_loads_object(txt) if txt else {}
        except (ValueError, OSError):
            doc = {}
    servers = json_object(doc.setdefault("mcpServers", {}))
    servers[server_name] = entry
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    return path, spec_path


def remove_mcp_config(server_name: str = DEFAULT_SERVER_NAME, path: str | None = None) -> bool:
    """Remove a previously-registered server entry (leaves other servers intact)."""
    path = path or detect_config_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            txt = f.read().strip()
        doc = json_loads_object(txt) if txt else {}
    except (ValueError, OSError):
        return False
    servers = json_object(doc.get("mcpServers", {}))
    if server_name in servers:
        del servers[server_name]
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
        return True
    return False


# --- arbitrary third-party servers (Blender MCP and friends) ------------------
def normalize_server_spec(spec: Mapping[str, object], unset_pythonhome: bool = True) -> "MCPServerSpec":
    """Normalize one arbitrary ``{command, args, env}`` server spec for agy.

    Policy lives here: agy inherits pyagy's child environment — including the
    ``PYTHONHOME`` the launcher sets for its embedded interpreter — and hands it
    to every MCP server it spawns, which silently breaks servers that start a
    different Python (``uv run``'s venv interpreter). So by default the command
    is wrapped in ``/usr/bin/env -u PYTHONHOME`` (idempotent; pre-wrapped specs
    pass through unchanged).
    """
    from wirecap.runtime import mcp as _mcp
    if unset_pythonhome:
        return _mcp.env_wrapped(spec)
    return _mcp.normalize_server_spec(spec)


def write_mcp_servers(servers: Mapping[str, Mapping[str, object]], *, path: str | None = None,
                      merge: bool = True, unset_pythonhome: bool = True) -> str:
    """Register arbitrary MCP servers (``{name: {command, args, env}}``) with agy.

    Unlike :func:`write_mcp_config` — which renders TaskSolver's own
    tools/context server — this writes caller-supplied specs verbatim (after
    :func:`normalize_server_spec`). ``merge=True`` preserves other entries in an
    existing config; ``path`` defaults to :func:`detect_config_path` (the GLOBAL
    store — concurrent sessions with colliding names race there, so scope the
    store via a ``data_dir`` home and pass its config path for parallel runs).
    Returns the config path.
    """
    from wirecap.runtime import mcp as _mcp
    path = path or detect_config_path()
    normalized = {name: normalize_server_spec(spec, unset_pythonhome)
                  for name, spec in servers.items()}
    return _mcp.write_mcp_servers_json(normalized, path, merge=merge)


def remove_mcp_servers(names: Sequence[str], path: str | None = None) -> int:
    """Bulk :func:`remove_mcp_config` for caller-registered servers. Returns the
    number of entries actually removed."""
    return sum(1 for name in names if remove_mcp_config(name, path=path))


def validate_server(spec_path: str | None = None, tools: Sequence[ToolSpec | ToolInput] | None = None,
                    context: Sequence[ContextResource | ContextInput] | None = None,
                    timeout: float = 15) -> dict[str, list[str]]:
    """Spawn ``agy_mcp_server.py`` and run the MCP handshake (initialize, tools/list,
    resources/list) directly, without agy — a fast pre-flight that the server starts
    and advertises the expected tools. Returns ``{"tools": [...], "resources": [...]}``.
    Provide either an existing ``spec_path`` or ``tools``/``context`` (rendered to a
    temp spec). Raises on protocol error or when the total handshake exceeds
    ``timeout`` seconds; the temporary server is always terminated and reaped."""
    import math
    import selectors
    import tempfile
    import time
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    deadline = time.monotonic() + timeout
    tmp = None
    proc: subprocess.Popen[bytes] | None = None
    try:
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
            [sys.executable, SERVER_SCRIPT], env=env, bufsize=0,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert proc.stdin is not None and proc.stdout is not None
        stdin, stdout = proc.stdin, proc.stdout
        os.set_blocking(stdout.fileno(), False)
        received = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)

            def call(mid: int, method: str) -> JSONObject:
                # Three fixed, small requests cannot fill the child's stdin pipe.
                stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                                         "params": {}}) + "\n").encode())
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"MCP server validation exceeded {timeout:g} seconds")
                    end = received.find(b"\n")
                    if end >= 0:
                        line = bytes(received[:end])
                        del received[:end + 1]
                        return json_loads_object(line)
                    if not selector.select(remaining):
                        continue
                    try:
                        chunk = os.read(stdout.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        raise RuntimeError("MCP server closed the stream during handshake")
                    received.extend(chunk)

            init = call(1, "initialize")
            if "result" not in init:
                raise RuntimeError(f"initialize failed: {init}")
            tlist = json_array(json_object(call(2, "tools/list").get("result", {})).get("tools", []))
            rlist = json_array(json_object(call(3, "resources/list").get("result", {})).get("resources", []))
            return {"tools": [name for t in tlist if isinstance(name := json_object(t).get("name"), str)],
                    "resources": [uri for r in rlist if isinstance(uri := json_object(r).get("uri"), str)]}
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=min(timeout, 0.2))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdin is not None:
                proc.stdin.close()
            if proc.stdout is not None:
                proc.stdout.close()
        if tmp:
            os.remove(tmp)
