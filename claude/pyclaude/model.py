"""TaskSolver's Claude Agent SDK adapter and legacy Claude Code contract."""

from __future__ import annotations

import base64
from typing import Any, Final

from tasksolver.cli_backend import CLIBackendModel
from tasksolver.common import Question, TaskSpec

from .client import Session, ask_many

_DEFAULT_TOOLS: Final = object()


def _image_source(url: str) -> dict[str, str]:
    if not url.startswith("data:"):
        return {"type": "url", "url": url}
    header, separator, encoded = url.partition(",")
    if not separator or not header.endswith(";base64"):
        raise ValueError("Claude image inputs need base64 data URLs")
    data: Final = base64.b64decode(encoded, validate=True)
    # Question currently labels every data URL image/jpeg, even its PNG encoder.
    # Determine the actual media type instead of propagating that incorrect label.
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        media_type = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        media_type = "image/webp"
    else:
        raise ValueError("Claude image inputs must be PNG, JPEG, GIF, or WebP")
    return {"type": "base64", "media_type": media_type, "data": encoded}


class ClaudeAgentModel(CLIBackendModel):
    """TaskSolver adapter; existing ``claude-code`` aliases use this SDK backend.

    ``allowed_tools`` retains the old adapter's built-in tool restriction as
    well as preapproving those tools. Pass ``tools=`` to select built-ins
    separately from permission rules (for example when allowing MCP tools).
    """

    backend_label = "LLM"
    command_label = "Claude Agent SDK"
    generic_model_aliases = ("claude-code", "claude-agent", "claude-sdk")
    no_output_hint = "Configure Claude authentication and install TaskSolver's SDK dependency."
    _client_ask_many = staticmethod(ask_many)

    def __init__(self, api_key: str | None = None, task: TaskSpec | None = None,
                 model: str | None = None, *, workspace: str | None = None,
                 timeout: float | None = 300, mcp_servers: dict | None = None,
                 allowed_tools: str | list[str] | None = "Read",
                 permission_mode: str = "acceptEdits",
                 tools: Any = _DEFAULT_TOOLS,
                 extra_env: dict[str, str] | None = None,
                 sdk_options: dict[str, Any] | None = None):
        super().__init__(api_key=api_key, task=task, model=model)
        self.claude_key = api_key
        self.thinking_depth = None
        self.workspace = workspace
        self.timeout = timeout
        self.mcp_servers = mcp_servers
        self.allowed_tools = allowed_tools
        self.permission_mode = permission_mode
        # The default matches legacy --tools <allowed_tools>. An explicit tools
        # value allows callers to distinguish availability from auto-approval.
        self.tools = allowed_tools if tools is _DEFAULT_TOOLS else tools
        self.extra_env = extra_env
        self.sdk_options = sdk_options

    @classmethod
    def prepare_payload(cls, question: Question, max_tokens=1000, verbose=False,
                        prepend=None, workspace=None, **kwargs) -> dict:
        content: Final = []
        for element in question.get_json():
            if element["type"] == "text":
                content.append({"type": "text", "text": element["text"]})
            elif element["type"] == "image_url":
                content.append({"type": "image",
                                "source": _image_source(element["image_url"]["url"])})
        # max_tokens remains part of TaskSolver's payload contract. The Agent
        # SDK does not provide a per-answer max_tokens option.
        return {"prompt": content, "max_tokens": max_tokens, "workspace": workspace}

    def _call_kwargs(self, payload: dict) -> dict:
        options: Final = dict(self.sdk_options or {})
        if self.thinking_depth:
            options["effort"] = self.thinking_depth
        return dict(model=self.model, workspace=payload.get("workspace") or self.workspace,
                    timeout=self.timeout, api_key=self.claude_key, extra_env=self.extra_env,
                    mcp_servers=self.mcp_servers, tools=self.tools,
                    allowed_tools=self.allowed_tools, permission_mode=self.permission_mode,
                    sdk_options=options)

    def _finish(self, r) -> dict:
        self._check_output(r)
        return r.metadata()

    def session(self, **kwargs) -> Session:
        options: Final = self._call_kwargs({})
        options.update(kwargs)
        return Session(**options)
