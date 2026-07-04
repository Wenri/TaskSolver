"""pyagy — drive the Antigravity `agy` CLI as a TaskSolver-contract backend.

- `Session` : **the first-class object** — a multi-turn conversation. In-run turns ride one
                live `agy` process; across restarts, agy's native store keeps context.
                `resume(id)` / `continue_latest()` reopen a stored conversation, `s.conversation_id`
                is its resumable id, `s.history()` its stored transcript, and
                `list_conversations()` enumerates past ones.
- `ask` : one-shot sugar over a transient Session — run a turn and get a decoded
                `AgyResponse`. One run installs the full hook union, so it captures the
                wire turn (`.turns`), app answer (`.app_text`), and RPC timeline
                (`.rpc_trace`) together; the `stack=`/`arg_probe=` overlays add symbolized
                stacks (`.stacks`/`.call_graph`) and the arg-graph diagnostic
                (`.cgt_args`). `.conversation_id` is resumable.
- `AgyModel`  : TaskSolver adapter (prepare_payload/ask/rough_guess/run_once/…),
                mirroring tasksolver.claude_code.ClaudeCodeModel.
- `run_print` : one-shot `agy --print` under a PTY (used by AgyModel).
- `InteractiveSession` : drive a multi-turn agy TUI session.
- `agy_process` : in-process recorder imported by the LD_PRELOADed shim.
- `HOOKS`/`by_mech`/`by_kind`/`enabled_hooks`/`sync_capable`/`DERIVED_KINDS` : the
                machine-readable hook→kind catalog (mirror of src/proc.def), for
                introspecting which hooks are installed (by mechanism) and which kinds
                can rewrite egress.

Top-level names are loaded lazily (PEP 562): `import pyagy.agy_process` — done by the
shim under a minimal *system* libpython — runs this __init__, so it must NOT eagerly
import model/session (they pull tasksolver, which that embedded interpreter can't load).
The catalog names below live in `agy_process.hooks` (stdlib-pure), also lazily.
"""
_LAZY = {
    # high-level client (the end-user API); Session is the first-class object
    "Session": ".client",
    "ask": ".client",
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
    # TaskSolver backend + lower-level drivers
    "AgyModel": ".model",
    "run_print": ".session",
    "InteractiveSession": ".session",
    "ensure_git_workspace": ".session",
    "strip_ansi": ".session",
    # config injection
    "write_mcp_config": ".config",
    "detect_config_path": ".config",
    # hook catalog (stdlib-pure; safe under the embedded interpreter)
    "HOOKS": ".agy_process.hooks",
    "DERIVED_KINDS": ".agy_process.hooks",
    "by_mech": ".agy_process.hooks",
    "enabled_hooks": ".agy_process.hooks",
    "by_kind": ".agy_process.hooks",
    "sync_capable": ".agy_process.hooks",
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
