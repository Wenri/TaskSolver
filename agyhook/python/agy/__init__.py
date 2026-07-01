"""agy — drive the Antigravity `agy` CLI as a TaskSolver-contract backend.

- `AgyModel`  : TaskSolver adapter (prepare_payload/ask/rough_guess/run_once/…),
                mirroring tasksolver.claude_code.ClaudeCodeModel.
- `run_print` : one-shot `agy --print` under a PTY (used by AgyModel).
- `InteractiveSession` : drive a multi-turn agy TUI session.
"""
from .model import AgyModel
from .session import InteractiveSession, ensure_git_workspace, run_print, strip_ansi

__all__ = ["AgyModel", "run_print", "InteractiveSession", "ensure_git_workspace", "strip_ansi"]
