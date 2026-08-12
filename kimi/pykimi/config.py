"""kimi-code store configuration written from the outside — the pykimi counterpart of
pycodex.config: MCP server registration plus the workspace-trust seed.

kimi-code takes its MCP servers from ``mcpServers`` JSON documents it discovers on disk
(``$KIMI_CODE_HOME/mcp.json``, the project root's ``.mcp.json``, ``<cwd>/.kimi-code/mcp.json`` —
there is no MCP flag); the rendering lives in ``wirecap.runtime.mcp``. The policy applied here
mirrors :func:`pycodex.config.mcp_flags`: the launcher exports ``PYTHONHOME`` for the embedded
interpreter, the CLI hands its environment to every MCP server it spawns, and that silently
breaks servers that start a different Python (``uv run``'s venv interpreter) — so by default
every server command is wrapped ``/usr/bin/env -u PYTHONHOME …`` (idempotent).
"""

import hashlib
import json
import os
import re
import time

from wirecap.runtime.mcp import env_wrapped, kimi_mcp_json, kimi_mcp_toml_lines, \
    normalize_server_spec

from . import sessions as _sessions

__all__ = ["mcp_json", "kimi_mcp_toml_lines", "encode_workdir_key",
           "trust_workspace"]


def mcp_json(servers, unset_pythonhome=True, indent=2):
    """The ``{"mcpServers": …}`` JSON document for arbitrary ``{name: {command, args, env}}``
    specs, PYTHONHOME-unwrapped by default (see module docstring). Write it to one of the paths
    the CLI discovers — :func:`pykimi.ask` uses the workspace-local ``.kimi-code/mcp.json``."""
    normalized = {name: (env_wrapped(spec) if unset_pythonhome
                         else normalize_server_spec(spec))
                  for name, spec in servers.items()}
    return kimi_mcp_json(normalized, indent=indent)


# --- workspace trust (the shell UI's pre-session gate) ------------------------
# Stdlib port of agent-core-v2's working-directory identity
# (`_base/utils/workdir-slug.ts`) and of WorkspaceTrustService's on-disk
# contract: one JSON document per workspace under the `workspace-trust` scope
# of the home-rooted atomic document store — the document's PRESENCE is the
# trusted state, its value `{root, trustedAt}` kept for inspection.

_MAX_WORKDIR_SLUG_LENGTH = 40


def _slugify_workdir_name(name):
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower())
    slug = re.sub(r"^-+|-+$", "", slug)[:_MAX_WORKDIR_SLUG_LENGTH]
    slug = re.sub(r"^-+|-+$", "", slug)
    return "workspace" if slug in ("", ".", "..") else slug


def encode_workdir_key(work_dir):
    """The stable, opaque workspace id kimi-code derives for a working directory
    (``wd_<slug>_<sha256[:12]>``) — the key of its session buckets and trust
    records. Byte-exact port of ``encodeWorkDirKey`` (workdir-slug.ts)."""
    normalized = re.sub(r"/+$", "", str(work_dir).replace("\\", "/"))
    base = normalized.split("/")[-1]
    slug = _slugify_workdir_name(base)
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:12]
    return f"wd_{slug}_{digest}"


def trust_workspace(workspace, home=None):
    """Pre-trust ``workspace`` in the kimi store rooted at ``home`` so the shell
    UI's folder-trust dialog never renders (a Session would otherwise block on it
    before the first turn; project-level MCP config is also gated on trust).

    Writes ``<home>/workspace-trust/<encodeWorkDirKey(workspace)>`` with the
    record shape ``WorkspaceTrustService.trust()`` writes (compact JSON
    ``{root, trustedAt}``). Idempotent: an existing record is left untouched —
    presence is the whole contract. The record deliberately lives under the
    HOME, never inside the workspace, matching the CLI (a checked-out tree
    cannot pre-trust itself). Returns the record path."""
    root = os.path.abspath(workspace)
    trust_dir = os.path.join(_sessions.home_root(home), "workspace-trust")
    path = os.path.join(trust_dir, encode_workdir_key(root))
    if not os.path.exists(path):
        os.makedirs(trust_dir, exist_ok=True)
        doc = json.dumps({"root": root, "trustedAt": int(time.time() * 1000)},
                         separators=(",", ":"))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(doc)
        os.replace(tmp, path)
    return path
