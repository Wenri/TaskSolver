"""Claude Agent SDK client and TaskSolver adapter, imported on demand."""

from importlib import import_module
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .client import ClaudeAgentError, ClaudeResponse, Session, ask, ask_async, ask_many
    from .model import ClaudeAgentModel

_LAZY: Final = {
    "ask": ".client",
    "ask_async": ".client",
    "ask_many": ".client",
    "Session": ".client",
    "ClaudeResponse": ".client",
    "ClaudeAgentError": ".client",
    "ClaudeAgentModel": ".model",
}


def __getattr__(name):
    module: Final = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module, __name__), name)


def __dir__():
    return sorted(_LAZY)


__all__ = ["ask", "ask_async", "ask_many", "Session", "ClaudeResponse",
           "ClaudeAgentError", "ClaudeAgentModel"]
