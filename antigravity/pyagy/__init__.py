"""pyagy — drive the Antigravity `agy` CLI as a TaskSolver-contract backend.

- `ask` / `Session` : the end-user client. Run a turn and get a decoded
                `AgyResponse`; `stage=` (int or alias `"wire"`/`"app"`/`"rpc"`/`"smoke"`)
                and the `stack=`/`arg_probe=` overlays choose what it captures —
                wire turn (`.turns`), app answer (`.app_text`), RPC timeline
                (`.rpc_trace`), symbolized stacks (`.stacks`/`.call_graph`), or the
                arg-graph diagnostic (`.cgt_args`).
- `AgyModel`  : TaskSolver adapter (prepare_payload/ask/rough_guess/run_once/…),
                mirroring tasksolver.claude_code.ClaudeCodeModel.
- `run_print` : one-shot `agy --print` under a PTY (used by AgyModel).
- `InteractiveSession` : drive a multi-turn agy TUI session.
- `agy_process` : in-process recorder imported by the LD_PRELOADed shim.
- `HOOKS`/`by_stage`/`by_kind`/`sync_capable`/`DERIVED_KINDS` : the machine-readable
                stage→hook→kind catalog (mirror of src/proc.def), for introspecting
                which stage captures what and which kinds can rewrite egress.

Top-level names are loaded lazily (PEP 562): `import pyagy.agy_process` — done by the
shim under a minimal *system* libpython — runs this __init__, so it must NOT eagerly
import model/session (they pull tasksolver, which that embedded interpreter can't load).
The catalog names below live in `agy_process.hooks` (stdlib-pure), also lazily.
"""
_LAZY = {
    # high-level client (the end-user API)
    "ask": ".client",
    "Session": ".client",
    "AgyResponse": ".client",
    "Usage": ".client",
    "ToolSpec": ".client",
    "ContextResource": ".client",
    "RewriteRule": ".client",
    # TaskSolver backend + lower-level drivers
    "AgyModel": ".model",
    "run_print": ".session",
    "InteractiveSession": ".session",
    "ensure_git_workspace": ".session",
    "strip_ansi": ".session",
    # config injection
    "write_mcp_config": ".config",
    "detect_config_path": ".config",
    # hook/stage catalog (stdlib-pure; safe under the embedded interpreter)
    "HOOKS": ".agy_process.hooks",
    "DERIVED_KINDS": ".agy_process.hooks",
    "by_stage": ".agy_process.hooks",
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
