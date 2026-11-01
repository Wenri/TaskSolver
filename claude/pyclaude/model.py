"""TaskSolver's Claude Agent SDK adapter, including the public Claude Code aliases."""

from __future__ import annotations

import base64
import copy
import functools
import tempfile
from typing import Final, Unpack

from wirecap.types import JSONObject
from .types import AskOptions, MCPServers, Tools

from tasksolver.cli_backend import CLIPayload, CLIBackendModel
from tasksolver.common import Question, TaskSpec, image_media_type

from .client import ClaudeResponse, Session, ask_many


class _DefaultTools:
    pass


_DEFAULT_TOOLS: Final = _DefaultTools()

#: Claude Code environment for chat mode: no auto-memory, CLAUDE.md, bundled/policy skills, git
#: instructions, claude.ai connector MCP servers, or non-essential traffic. Naming the session
#: suppresses automatic session-title requests.
CHAT_ENV: Final = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
    "CLAUDE_CODE_SESSION_NAME": "tasksolver-chat",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
}

#: SDK options for chat mode: no filesystem settings (hooks, plugins, permissions, CLAUDE.md),
#: no skills, one turn, and no transcript under ~/.claude/projects.
CHAT_SDK_OPTIONS: Final[dict[str, object]] = {
    "setting_sources": [],
    "skills": [],
    "max_turns": 1,
    "extra_args": {"no-session-persistence": None},
}

#: content-block types that mean the model acted as an agent
_TOOL_BLOCKS: Final = ("tool_use", "server_tool_use", "mcp_tool_use")


@functools.cache
def _chat_workspace() -> str:
    """One empty working directory per process for chat-mode models without a workspace."""
    return tempfile.mkdtemp(prefix="tasksolver-claude-chat-")


def thinking_config(thinking: str | bool | int | JSONObject | None) -> JSONObject | None:
    """SDK ``thinking`` from a short form: ``"disabled"``/``False``, ``"adaptive"``/``True``,
    an int budget (``{"type": "enabled", "budget_tokens": n}``), a config dict, or ``None``
    (the CLI default)."""
    if thinking is None or isinstance(thinking, dict):
        return thinking
    if thinking is False or thinking in ("disabled", "off", "none"):
        return {"type": "disabled"}
    if thinking is True or thinking in ("adaptive", "on"):
        return {"type": "adaptive"}
    if isinstance(thinking, int) and thinking > 0:
        return {"type": "enabled", "budget_tokens": thinking}
    raise ValueError(f"unsupported thinking setting {thinking!r}")


def _image_source(url: str) -> JSONObject:
    if not url.startswith("data:"):
        return {"type": "url", "url": url}
    header, separator, encoded = url.partition(",")
    if not separator or not header.endswith(";base64"):
        raise ValueError("Claude image inputs need base64 data URLs")
    data: Final = base64.b64decode(encoded, validate=True)
    # Validate the media type from the bytes; a data URL's MIME label can be wrong.
    media_type = image_media_type(data, default="")
    if not media_type:
        raise ValueError("Claude image inputs must be PNG, JPEG, GIF, or WebP")
    return {"type": "base64", "media_type": media_type, "data": encoded}


class ClaudeAgentModel(CLIBackendModel[ClaudeResponse]):
    """TaskSolver adapter for the ``claude-code`` and ``claude-agent`` aliases.

    When ``tools`` is omitted, ``allowed_tools`` both selects built-ins and
    preapproves them. Set ``tools`` to configure availability separately from
    permission rules, for example when allowing MCP tools.

    ``chat=True`` makes each call a plain chat turn: no built-in tools, no MCP
    servers (``--strict-mcp-config`` with none), no filesystem settings, skills
    or memory, one turn, no saved transcript, thinking disabled and effort
    ``"low"`` unless ``thinking``/``effort`` say otherwise, and a private empty
    working directory unless ``workspace`` is given. A reply containing a tool
    call raises ``ChatModeViolation``. What remains of Claude Code is its
    one-line SDK system prompt and a few context reminders (working directory,
    model name, account email, date) in the user turn.

    ``thinking`` takes the forms of :func:`thinking_config`; ``effort`` is the
    SDK effort level; ``system_prompt`` replaces the SDK's default one.
    """

    backend_label = "LLM"
    command_label = "Claude Agent SDK"
    generic_model_aliases = ("claude-code", "claude-agent", "claude-sdk")
    no_output_hint = "Configure Claude authentication and install TaskSolver's SDK dependency."
    _client_ask_many = staticmethod(ask_many)

    def __init__(self, api_key: str | None = None, task: TaskSpec | None = None,
                 model: str | None = None, *, workspace: str | None = None,
                 timeout: float | None = 300, mcp_servers: MCPServers | None = None,
                 allowed_tools: str | list[str] | None = "Read",
                 permission_mode: str = "acceptEdits",
                 tools: Tools | _DefaultTools = _DEFAULT_TOOLS,
                 extra_env: dict[str, str] | None = None,
                 sdk_options: dict[str, object] | None = None,
                 chat: bool = False, thinking: str | bool | int | JSONObject | None = None, effort: str | None = None,
                 system_prompt: str | None = None) -> None:
        super().__init__(api_key=api_key, task=task, model=model)
        if chat:
            if mcp_servers:
                raise ValueError("chat mode runs without MCP servers")
            if tools is not _DEFAULT_TOOLS and tools:
                raise ValueError("chat mode runs without tools")
            tools, allowed_tools, mcp_servers, permission_mode = [], [], {}, "default"
            thinking = "disabled" if thinking is None else thinking
            effort = effort or "low"
            extra_env = {**CHAT_ENV, **(extra_env or {})}
            sdk_options = {**copy.deepcopy(CHAT_SDK_OPTIONS), **(sdk_options or {})}
            if workspace is None:
                # Keep the caller's working tree out of the CLI's environment reminder.
                workspace = _chat_workspace()
        self.chat = chat
        self.claude_key = api_key
        # Public attribute storing the SDK effort level.
        self.thinking_depth = effort
        self.thinking = thinking_config(thinking)
        self.system_prompt = system_prompt
        self.workspace = workspace
        self.timeout = timeout
        self.mcp_servers = mcp_servers
        self.allowed_tools = allowed_tools
        self.permission_mode = permission_mode
        # Explicit tools separate availability from auto-approval.
        self.tools: Tools = allowed_tools if isinstance(tools, _DefaultTools) else tools
        self.extra_env = extra_env
        self.sdk_options = sdk_options

    @classmethod
    def prepare_payload(cls, question: Question, max_tokens: int = 1000, verbose: bool = False,
                        prepend: object = None, workspace: str | None = None, inline: bool = False,
                        **kwargs: object) -> CLIPayload:
        content: list[JSONObject] = []
        for element in question.get_json():
            if element["type"] == "text":
                content.append({"type": "text", "text": element["text"]})
            elif element["type"] == "image_url":
                content.append({"type": "image",
                                "source": _image_source(element["image_url"]["url"])})
        # max_tokens remains part of TaskSolver's payload contract. The Agent
        # SDK does not provide a per-answer max_tokens option.
        return {"prompt": content, "max_tokens": max_tokens, "workspace": workspace}

    def _sample(self, payload: CLIPayload, n_choices: int) -> list[ClaudeResponse]:
        return self._client_ask_many(payload["prompt"], n_choices, **self._call_kwargs(payload))

    def _call_kwargs(self, payload: CLIPayload | None = None) -> AskOptions:
        options: Final = dict(self.sdk_options or {})
        if self.thinking_depth:
            options["effort"] = self.thinking_depth
        if self.thinking is not None:
            options["thinking"] = self.thinking
        if self.system_prompt is not None:
            options["system_prompt"] = self.system_prompt
        return {"model": self.model,
                "workspace": (payload.get("workspace") if payload is not None else None) or self.workspace,
                "timeout": self.timeout, "api_key": self.claude_key, "extra_env": self.extra_env,
                "mcp_servers": self.mcp_servers, "tools": self.tools,
                "allowed_tools": self.allowed_tools, "permission_mode": self.permission_mode,
                "sdk_options": options}

    def _finish(self, r: ClaudeResponse) -> dict[str, object]:
        self._check_output(r)
        if self.chat:
            self._check_chat(r)
        return r.metadata()

    def _chat_violations(self, r: ClaudeResponse) -> list[str]:
        violations: list[str] = []
        for message in r.messages:
            content = message.get("content")
            if message.get("type") != "assistant" or not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") in _TOOL_BLOCKS:
                    violations.append(str(block.get("name") or block.get("type")))
        return violations

    def session(self, **kwargs: Unpack[AskOptions]) -> Session:
        options: AskOptions = self._call_kwargs()
        options.update(kwargs)
        return Session(**options)
