"""Compatible Claude Code adapter, implemented by the official Claude Agent SDK.

Existing imports and ``claude-code*`` model IDs remain supported. The shared
implementation also exposes direct queries and resumable sessions via pyclaude.
"""

from pyclaude.model import ClaudeAgentModel


class ClaudeCodeModel(ClaudeAgentModel):
    """Backward-compatible name for the SDK-backed Claude adapter."""
