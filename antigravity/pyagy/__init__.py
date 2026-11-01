"""pyagy — drive the Antigravity `agy` CLI as a TaskSolver-contract backend.

- `Session` : **the first-class object** — a multi-turn conversation. In-run turns ride one
                live `agy` process; across restarts, agy's native store keeps context.
                `resume(id)` / `continue_latest()` reopen a stored conversation, `s.conversation_id`
                is its resumable id, `s.history()` its stored transcript, and
                `list_conversations()` enumerates past ones.
- `ask` : one-shot sugar over a transient Session — run a turn and get a decoded
                `AgyResponse`. One run installs the full book union, so it captures the
                wire turn (`.turns`), app answer (`.app_text`), and RPC timeline
                (`.rpc_trace`) together; the `stack=`/`arg_probe=` overlays add symbolized
                stacks (`.stacks`/`.call_graph`) and the arg-graph diagnostic
                (`.cgt_args`). `.conversation_id` is resumable.
- `AgyModel`  : TaskSolver adapter (prepare_payload/ask/rough_guess/run_once/…),
                mirroring tasksolver.claude_code.ClaudeCodeModel.
- `agy_process` : in-process recorder imported by the preloaded shim.
- `BOOKS`/`by_mech`/`by_kind`/`enabled_books`/`sync_capable`/`DERIVED_KINDS` : the
                machine-readable mirror of BOOKS, the book table in src/procdef.h, for
                introspecting which books are installed (by mechanism) and which kinds
                can rewrite egress.

Top-level names are loaded lazily (PEP 562): `import pyagy.agy_process` — done by the
shim under a minimal *system* libpython — runs this __init__, so it must NOT eagerly
import model (it pulls tasksolver, which that embedded interpreter can't load).
The catalog names below live in `agy_process.books` (stdlib-pure), also lazily.
"""
_LAZY = {
    # high-level client (the end-user API); Session is the first-class object
    "Session": ".client",
    "ask": ".client",
    "ask_many": ".client",
    "resume": ".client",
    "continue_latest": ".client",
    "AgyResponse": ".client",
    "Usage": ".client",
    "ToolSpec": ".client",
    "ContextResource": ".client",
    "RewriteRule": ".client",
    # native conversation store (read-only readers + trust/scope write helpers; stdlib-pure)
    "list_conversations": ".conversations",
    "latest_conversation_id": ".conversations",
    "read_transcript": ".conversations",
    "ConversationInfo": ".conversations",
    "trust_workspace": ".conversations",
    "prepare_scoped_home": ".conversations",
    "seed_onboarding": ".conversations",
    # TaskSolver backend + lower-level drivers
    "AgyModel": ".model",
    "ensure_git_workspace": ".conversations",
    "strip_ansi": "._term",
    # config injection
    "write_mcp_config": ".config",
    "remove_mcp_config": ".config",
    "write_mcp_servers": ".config",
    "remove_mcp_servers": ".config",
    "detect_config_path": ".config",
    # the BOOKS mirror (stdlib-pure; safe under the embedded interpreter)
    "BOOKS": ".agy_process.books",
    "DERIVED_KINDS": ".agy_process.books",
    "by_mech": ".agy_process.books",
    "enabled_books": ".agy_process.books",
    "by_kind": ".agy_process.books",
    "sync_capable": ".agy_process.books",
}
__all__ = list(_LAZY)


def __getattr__(name):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(mod, __name__), name)


def __dir__():
    return sorted(__all__)
