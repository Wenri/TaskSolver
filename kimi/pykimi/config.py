"""MCP server registration for kimi-code — the pykimi counterpart of pycodex.config.

kimi-code takes its MCP servers from ``mcpServers`` JSON documents it discovers on disk
(``$KIMI_CODE_HOME/mcp.json``, the project root's ``.mcp.json``, ``<cwd>/.kimi-code/mcp.json`` —
there is no MCP flag); the rendering lives in ``wirecap.runtime.mcp``. The policy applied here
mirrors :func:`pycodex.config.mcp_flags`: the launcher exports ``PYTHONHOME`` for the embedded
interpreter, the CLI hands its environment to every MCP server it spawns, and that silently
breaks servers that start a different Python (``uv run``'s venv interpreter) — so by default
every server command is wrapped ``/usr/bin/env -u PYTHONHOME …`` (idempotent).
"""

from wirecap.runtime.mcp import env_wrapped, kimi_mcp_json, kimi_mcp_toml_lines, \
    normalize_server_spec

__all__ = ["mcp_json", "kimi_mcp_toml_lines"]


def mcp_json(servers, unset_pythonhome=True, indent=2):
    """The ``{"mcpServers": …}`` JSON document for arbitrary ``{name: {command, args, env}}``
    specs, PYTHONHOME-unwrapped by default (see module docstring). Write it to one of the paths
    the CLI discovers — :func:`pykimi.ask` uses the workspace-local ``.kimi-code/mcp.json``."""
    normalized = {name: (env_wrapped(spec) if unset_pythonhome
                         else normalize_server_spec(spec))
                  for name, spec in servers.items()}
    return kimi_mcp_json(normalized, indent=indent)
