"""pyagy — drive the Antigravity `agy` CLI as a TaskSolver-contract backend.

- `AgyModel`  : TaskSolver adapter (prepare_payload/ask/rough_guess/run_once/…),
                mirroring tasksolver.claude_code.ClaudeCodeModel.
- `run_print` : one-shot `agy --print` under a PTY (used by AgyModel).
- `InteractiveSession` : drive a multi-turn agy TUI session.
- `agy_process` : in-process recorder imported by the LD_PRELOADed shim.

Top-level names are loaded lazily (PEP 562): `import pyagy.agy_process` — done by the
shim under a minimal *system* libpython — runs this __init__, so it must NOT eagerly
import model/session (they pull tasksolver, which that embedded interpreter can't load).
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
