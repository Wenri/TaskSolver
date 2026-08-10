"""MCP server registration for codex — the pycodex counterpart of pyagy.config.

codex takes its MCP servers as ``-c mcp_servers.<name>=<inline TOML>`` config
flags; the rendering lives in ``wirecap.runtime.mcp``. The policy applied here
mirrors :func:`pyagy.config.normalize_server_spec`: the launcher exports
``PYTHONHOME`` for codex's embedded interpreter, codex hands its environment to
every MCP server it spawns, and that silently breaks servers that start a
different Python (``uv run``'s venv interpreter) — so by default every server
command is wrapped ``/usr/bin/env -u PYTHONHOME …`` (idempotent).
"""

from wirecap.runtime.mcp import codex_config_flags, env_wrapped, normalize_server_spec


def mcp_flags(servers, unset_pythonhome=True):
    """``-c mcp_servers.<name>=<TOML>`` flags for arbitrary ``{name: {command,
    args, env}}`` specs, PYTHONHOME-unwrapped by default (see module docstring).
    Server names must be bare TOML keys — enforced by the renderer."""
    normalized = {name: (env_wrapped(spec) if unset_pythonhome
                         else normalize_server_spec(spec))
                  for name, spec in servers.items()}
    return codex_config_flags(normalized)
