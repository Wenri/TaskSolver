"""pykimi — a provider wrapper for Moonshot's Kimi Code CLI, sibling to pycodex/pyagy.

Runs the vendored, wiretap-patched kimi-code (a Node CLI) with the wirecap bridge loaded
in-process: a small N-API addon (``kimi/native/wirecap_node.cc``) embeds CPython exactly like
codex's compiled-in bridge, and the vendored source carries a three-line patch that emits every
model request (``kimi_request``), every streamed model event (``kimi_event``) and every persisted
wire-journal record (``kimi_wire``) into it. The embedded worker decodes them into ``kimi_turn``s
written to the ``WIRE_CAPTURE`` JSONL (authoritative) and streamed home over the same mp-child
channel agy and codex use.

Public API:
    pykimi.ask("What is 2+2?")   -> KimiResponse (.text / .model / .usage / .request / .turns)
    pykimi.ask_many(prompt, n)   -> [KimiResponse, ...]
    pykimi.KimiCodeModel(...)    -> a TaskSolver-contract backend (pulls tasksolver — lazy)

Exports are LAZY (PEP 562): importing ``pykimi.kimi_process`` (the WIRE_MODULE the embedded
interpreter loads) runs this package __init__, which must stay import-pure — so ``client`` (and
especially ``model``, which imports ``tasksolver``) are imported only on attribute access, never
at package load. The in-process decode side is ``pykimi.kimi_process``.
"""

_LAZY = {
    "ask": ".client",
    "ask_many": ".client",
    "KimiResponse": ".client",
    "Usage": ".client",
    "KimiCodeModel": ".model",
    # re-exported so callers can seed a workspace before ask() — same name as pyagy/pycodex.
    "ensure_git_workspace": "wirecap.runtime.workspace",
    # kimi-code's native session store (read-only), the pycodex.sessions counterpart
    "latest_session_id": ".sessions",
    "find_session_dir": ".sessions",
    "read_wire": ".sessions",
    # MCP server registration (the pycodex.config counterpart): the mcpServers JSON
    # document with the PYTHONHOME unwrap kimi-spawned servers need
    "mcp_json": ".config",
    "kimi_mcp_toml_lines": ".config",
}


def __getattr__(name):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    return getattr(importlib.import_module(mod, __name__), name)


def __dir__():
    return sorted(_LAZY)


__all__ = sorted(_LAZY)   # so `from pykimi import *` binds the lazy names
