"""Claude Agent SDK client and TaskSolver adapter, imported on demand."""

from importlib import import_module
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from .client import ClaudeAgentError, ClaudeAgentTimeout, ClaudeResponse, Session, ask, ask_async, ask_many, attach_terminal
    from .model import ClaudeAgentModel
    from .transport import NativeTransport

_LAZY: Final = {
    "ask": ".client",
    "ask_async": ".client",
    "ask_many": ".client",
    "Session": ".client",
    "ClaudeResponse": ".client",
    "ClaudeAgentError": ".client",
    "ClaudeAgentTimeout": ".client",
    "ClaudeAgentModel": ".model",
    "NativeTransport": ".transport",
    "attach_terminal": ".client",
}


def __getattr__(name: str) -> object:
    module: Final = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return cast(object, getattr(import_module(module, __name__), name))


def __dir__() -> list[str]:
    return sorted(_LAZY)


__all__ = ["ask", "ask_async", "ask_many", "Session", "ClaudeResponse",
           "ClaudeAgentError", "ClaudeAgentTimeout", "ClaudeAgentModel", "NativeTransport", "attach_terminal"]
