"""MCP server-spec normalization + per-CLI config serialization.

The canonical spec is a plain dict ``{"command": str, "args": [str], "env":
{str: str}}`` — the shape every consumer ultimately wants (JSON config files,
codex ``-c`` TOML tables, ``StdioServerParameters(**spec)``). This module owns
only spec normalization and the per-CLI *serialization* of ``{name: spec}``
maps; policy that differs per harness — memory switches, auth material, home
layout, resume stores — stays with the caller.

Serializations are byte-compatible with the formats the CLIs read:

- generic / claude / kimi / agy: ``{"mcpServers": {name: spec}}`` JSON
- codex: one ``-c mcp_servers.<name>=<inline TOML table>`` flag per server
- qwen: the ``settings.json`` ``mcpServers`` value (spec + trust/timeout)

Parent-side only (like the rest of ``wirecap.runtime``); stdlib imports only.
"""
import json
import os
import re

# codex config keys are dotted paths; a server name must be a bare TOML key so
# `mcp_servers.<name>` needs no quoting. Hyphens and underscores are both fine.
BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def normalize_server_spec(spec):
    """Validate one spec and return a normalized copy.

    ``command`` is required; ``args`` defaults to ``[]`` and ``env`` to ``{}``,
    with everything coerced to strings. Unknown keys are preserved (some
    consumers extend the spec in place, e.g. qwen's ``trust``/``timeout``).
    """
    if not isinstance(spec, dict):
        raise TypeError(f"MCP server spec must be a dict, got {type(spec)!r}")
    command = spec.get("command")
    if not command or not isinstance(command, str):
        raise ValueError(f"MCP server spec needs a 'command' string: {spec!r}")
    out = dict(spec)
    out["command"] = command
    out["args"] = [str(a) for a in (spec.get("args") or [])]
    out["env"] = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
    return out


def env_wrapped(spec, unset=("PYTHONHOME",)):
    """Rewrite a spec to run under ``/usr/bin/env -u VAR ...`` for each ``unset`` var.

    Some launchers export interpreter-steering variables for their own embedded
    Python (pyagy sets ``PYTHONHOME`` for its shim) and the CLI hands that
    environment to the MCP servers it spawns, silently breaking any server that
    starts a different Python (``uv run``'s venv interpreter). Wrapping the
    command strips the variables for the server process only.

    Idempotent: a spec already wrapped with the same variables is returned
    unchanged, so re-normalizing provisioned specs is safe.
    """
    spec = normalize_server_spec(spec)
    unset = tuple(unset)
    if not unset:
        return spec
    wrap_prefix = [flag for var in unset for flag in ("-u", var)]
    if (spec["command"] == "/usr/bin/env"
            and spec["args"][:len(wrap_prefix)] == wrap_prefix):
        return spec
    return {**spec,
            "command": "/usr/bin/env",
            "args": [*wrap_prefix, spec["command"], *spec["args"]]}


def render_mcp_servers(servers):
    """``{"mcpServers": {name: normalized spec}}`` — the shared config document."""
    return {"mcpServers": {name: normalize_server_spec(spec)
                           for name, spec in servers.items()}}


def mcp_servers_json(servers, indent=2):
    return json.dumps(render_mcp_servers(servers), indent=indent)


def write_mcp_servers_json(servers, path, merge=False, indent=2):
    """Write (or merge into) an ``mcpServers`` JSON config file. Returns ``path``.

    ``merge=True`` preserves other top-level keys and other server entries in an
    existing file (the semantics agy's config store expects); ``merge=False``
    replaces the file with exactly these servers.
    """
    path = os.fspath(path)
    doc = {}
    if merge and os.path.exists(path):
        try:
            with open(path) as f:
                txt = f.read().strip()
            doc = json.loads(txt) if txt else {}
        except (ValueError, OSError):
            doc = {}
    entries = doc.setdefault("mcpServers", {})
    for name, spec in servers.items():
        entries[name] = normalize_server_spec(spec)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f, indent=indent)
    return path


def toml_value(value):
    """Render a JSON-ish value as inline TOML (strings via JSON escaping)."""
    if isinstance(value, str):
        return json.dumps(value)          # JSON strings are valid TOML strings
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {toml_value(v)}"
                               for k, v in value.items()) + "}"
    raise TypeError(f"cannot render {type(value)!r} as TOML")


def codex_config_flags(servers):
    """codex CLI flags: one ``-c mcp_servers.<name>=<inline TOML table>`` per server.

    Server names must be bare TOML keys (``[A-Za-z0-9_-]+``) because they ride
    in a dotted config path; anything else raises rather than producing a flag
    codex would misparse.
    """
    flags = []
    for name, spec in servers.items():
        if not BARE_KEY_RE.match(name):
            raise ValueError(
                f"MCP server name {name!r} is not a bare TOML key "
                "([A-Za-z0-9_-]+); rename the server for the codex backend")
        flags += ["-c", f"mcp_servers.{name}={toml_value(normalize_server_spec(spec))}"]
    return flags


def claude_mcp_args(servers, config_path, strict=True):
    """Write claude's MCP config file and return the CLI args that load it.

    ``--strict-mcp-config`` keeps the run from also inheriting whatever is in
    the user's ``~/.claude.json`` or a project ``.mcp.json``, so the session
    sees exactly these servers.
    """
    config_path = os.fspath(config_path)
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    with open(config_path, "w") as f:
        f.write(mcp_servers_json(servers))
    args = ["--mcp-config", config_path]
    if strict:
        args.append("--strict-mcp-config")
    return args


def qwen_mcp_servers(servers, trust=True, timeout_ms=60000):
    """The ``settings.json`` ``mcpServers`` value qwen code reads: each spec plus
    ``trust`` (skip the per-server confirmation) and a request ``timeout``."""
    return {name: {**normalize_server_spec(spec),
                   "trust": trust, "timeout": timeout_ms}
            for name, spec in servers.items()}


def kimi_mcp_json(servers, indent=2):
    """kimi code's ``$KIMI_CODE_HOME/mcp.json`` — the shared mcpServers document."""
    return mcp_servers_json(servers, indent=indent)


def kimi_mcp_toml_lines(startup_timeout_ms=120000, tool_timeout_ms=900000):
    """The ``[mcp]`` section for kimi code's ``config.toml``. Timeouts default high
    on purpose: ``uv run <server>`` may build its environment on the first call
    of a fresh checkout, and a single tool call can legitimately run for minutes."""
    return ["[mcp]",
            f"startup_timeout_ms = {int(startup_timeout_ms)}",
            f"tool_timeout_ms = {int(tool_timeout_ms)}"]
