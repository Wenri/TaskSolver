"""CodexModel — a TaskSolver-contract backend that drives the (linked) codex CLI.

The codex subclass of :class:`tasksolver.cli_backend.CLIBackendModel`, sibling of
``pyagy.AgyModel`` / ``tasksolver.claude_code.ClaudeCodeModel``: the official Python SDK drives
the linked app-server in a workspace. ``transport="exec"`` retains the legacy exec
transport. Unlike AgyModel, codex needs real auth — set
``OPENAI_API_KEY`` or ``codex login`` first (``api_key`` is passed through to the run env for the
API-key path).

    from tasksolver.common import TaskSpec, Question
    from pycodex import CodexModel
    model = CodexModel(api_key=None, task=my_task, model="gpt-5-codex")
    parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))

``chat=True`` (SDK transport) runs each call as a plain Responses turn: an ephemeral thread,
empty base instructions unless ``system_prompt`` is given, no tools and no injected context
(see :data:`CHAT_CONFIG`), inline images in the Question's order, and reasoning ``effort``
``"none"`` unless set. A turn that ran a command, edited a file or called a tool raises
``ChatModeViolation``.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Final, Protocol, Unpack, cast

from wirecap.runtime.mcp import MCPServers
from wirecap.types import JSONObject
from openai_codex._inputs import RunInput
from .types import ModelSessionOptions, SDKAskOptions, SDKSessionOptions, ThreadOptions

from tasksolver.cli_backend import CLIPayload, CLIBackendModel, string_prompt
from tasksolver.common import TaskSpec

from .client import (AskOptions, CodexResponse, Session, SessionOptions,
                     ask_many as _codex_ask_many)
from .sdk import CodexSDKResponse, SDKSession, ask_many_sdk

#: app-server config overrides for chat mode. With them (and :data:`CHAT_ENV`) the captured
#: Responses request carries ``tools: []``, empty ``instructions`` and only the user message
#: (checked against CLI 0.157.0 with a user config that enables plugins, memories, an MCP
#: server and "ultra" reasoning).
CHAT_CONFIG: Final[JSONObject] = {
    **{f"features.{name}": False for name in (
        "shell_tool", "unified_exec", "view_image", "apps", "plugins", "tool_search",
        "image_generation", "goals", "memories", "multi_agent", "js_repl", "code_mode", "hooks",
        "tool_suggest", "browser_use", "computer_use", "skill_search")},
    "web_search": "disabled",
    "tools.experimental_request_user_input.enabled": False,
    "include_permissions_instructions": False,
    "include_apps_instructions": False,
    "include_collaboration_mode_instructions": False,
    "include_environment_context": False,
    "project_doc_max_bytes": 0,
    "skills.include_instructions": False,
    "memories.use_memories": False,
    "memories.generate_memories": False,
    "model_reasoning_summary": "none",
}

#: no execution environment: removes the shell, apply_patch and view_image tools, which the
#: model catalog otherwise enables regardless of the feature flags
CHAT_ENV: Final[dict[str, str]] = {"CODEX_EXEC_SERVER_URL": "none"}

#: thread items a chat turn may contain; anything else (commands, file changes, tool or web
#: calls, plans, sub-agents, ...) means the model acted as an agent
_CHAT_ITEMS: Final = ("userMessage", "agentMessage", "reasoning")


def configured_mcp_servers(codex_home: str | None = None) -> list[str]:
    """Names of the MCP servers in ``<codex_home>/config.toml`` (default ``$CODEX_HOME`` or
    ``~/.codex``). Chat mode disables each one: an override can switch a server off, but
    cannot remove it from the user config."""
    import tomllib

    home: Final = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    try:
        with open(os.path.join(home, "config.toml"), "rb") as f:
            config = cast(dict[str, object], tomllib.load(f))
            servers = config.get("mcp_servers")
            if not isinstance(servers, Mapping):
                return []
            return list(cast(Mapping[str, object], servers))
    except (OSError, tomllib.TOMLDecodeError):
        return []


class _ExecSampler(Protocol):
    def __call__(self, prompt: str, n: int, **kwargs: Unpack[AskOptions]) -> list[CodexResponse]: ...


class _SDKSampler(Protocol):
    def __call__(self, prompt: RunInput, n: int, **kwargs: Unpack[SDKAskOptions]) -> list[CodexSDKResponse]: ...


class CodexModel(CLIBackendModel[CodexResponse]):
    backend_label = "codex"
    command_label = "codex exec"
    generic_model_aliases = ("codex", "codex-sdk")
    no_output_hint = ("Ensure codex is authenticated (OPENAI_API_KEY or `codex login`) and the "
                      "built binary exists (`pixi install`).")
    # codex has no `Read` tool — it reads files through its own shell/apply_patch sandbox — so it
    # keeps its own wording rather than the base's "Use the Read tool". A prompt-correctness
    # difference, not cosmetic: naming a tool the CLI does not have would degrade vision tasks.
    vision_preamble = "The visual inputs are saved as local image files; read them when answering."
    #: NOTE pycodex.ask_many is SEQUENTIAL (codex writes one fixed capture path per workspace), so
    #: many_rough_guesses(num_threads=n) costs n x wall time here, unlike agy's parallel sampling.
    _client_ask_many: object = staticmethod(_codex_ask_many)

    def __init__(self, api_key: str | None = None, task: TaskSpec | None = None,
                 model: str | None = None, workspace: str | None = None, timeout: int = 300,
                 mcp_servers: MCPServers | None = None,
                 codex_home: str | None = None, transport: str = "sdk",
                 sdk_options: SDKAskOptions | None = None,
                 chat: bool = False, effort: str | None = None,
                 system_prompt: str | None = None) -> None:
        # api_key = OPENAI_API_KEY for API-key auth (or None → codex login)
        super().__init__(api_key=api_key, task=task, model=model)
        if chat and transport != "sdk":
            raise ValueError("chat mode needs the SDK transport")
        if chat and mcp_servers:
            raise ValueError("chat mode runs without MCP servers")
        self.chat = chat
        # reasoning effort per turn ("none" … "xhigh"); chat mode defaults to "none"
        self.effort = effort
        # the thread's base instructions in chat mode (default: none)
        self.system_prompt = system_prompt
        self.workspace = workspace
        self.timeout = timeout
        # Arbitrary {name: {command, args, env}} MCP servers registered for every
        # run of this model (rendered to `-c mcp_servers.<name>=...` flags).
        self.mcp_servers = mcp_servers
        self.codex_home = codex_home          # scope codex's store (auth/sessions) per model
        if transport not in ("exec", "sdk"):
            raise ValueError("transport must be 'exec' or 'sdk'")
        self.transport = transport
        self.sdk_options: SDKAskOptions = {**(sdk_options or {})}
        if transport == "sdk":
            self._client_ask_many = ask_many_sdk
            self.command_label = "codex app-server"

    def _finish(self, r: CodexResponse) -> dict[str, object]:
        if self.transport == "sdk" and r.timed_out:
            raise TimeoutError(f"Codex SDK timed out after {self.timeout}s")
        result: Final = super()._finish(r)
        if self.transport == "sdk":
            sdk_response = cast(CodexSDKResponse, r)
            result.update(session_id=r.session_id, capture_path=r.capture_path,
                          timed_out=r.timed_out, turns=r.turns,
                          sdk_result=sdk_response.sdk_result,
                          explicit_reasoning_output=sdk_response.explicit_reasoning_output)
        return result

    def _chat_violations(self, r: CodexResponse) -> list[str]:
        sdk_response = cast(CodexSDKResponse, r)
        items = sdk_response.sdk_result.items if sdk_response.sdk_result is not None else []
        return [item.root.type for item in items if item.root.type not in _CHAT_ITEMS]

    def _sample(self, payload: CLIPayload, n_choices: int) -> Sequence[CodexResponse]:
        options = self._call_kwargs(payload)
        if self.transport == "exec":
            # The selected transport owns its callable and option schema.
            return cast(_ExecSampler, self._client_ask_many)(
                string_prompt(payload), n_choices, **cast(AskOptions, options))
        from openai_codex import ImageInput, InputItem, LocalImageInput, TextInput
        prompt: RunInput
        if self.chat and "content" in payload:
            prompt = [TextInput(text=part["text"]) if part["type"] == "text"
                      else ImageInput(url=part["url"]) for part in payload["content"]]
        elif self.chat:
            prompt = [TextInput(text=string_prompt(payload))]
        elif payload.get("image_paths"):
            inputs: list[InputItem] = [TextInput(text=string_prompt(payload))]
            inputs.extend(LocalImageInput(path=os.path.abspath(path))
                          for path in payload.get("image_paths", []))
            prompt = inputs
        else:
            prompt = string_prompt(payload)
        return cast(_SDKSampler, self._client_ask_many)(prompt, n_choices, **options)

    def _call_kwargs(self, payload: CLIPayload) -> SDKAskOptions:
        kw: SDKAskOptions = {"workspace": payload.get("workspace") or self.workspace,
                             "model": self.model, "timeout": self.timeout}
        if self.mcp_servers:
            kw["mcp_servers"] = self.mcp_servers
        if self.codex_home:
            kw["codex_home"] = self.codex_home
        if self.api_key:
            kw["extra_env"] = {"OPENAI_API_KEY": self.api_key}
        if self.transport == "sdk":
            kw.update(self.sdk_options)
        effort: Final = self.effort or ("none" if self.chat else None)
        if self.chat:
            config: JSONObject = {**CHAT_CONFIG, "model_reasoning_effort": effort,
                             **{f"mcp_servers.{name}.enabled": False
                                for name in configured_mcp_servers(self.codex_home)}}
            thread: ThreadOptions = {**(kw.get("thread_options") or {})}
            config.update(thread.pop("config", None) or {})
            kw["thread_options"] = {"ephemeral": True, "base_instructions": self.system_prompt or "",
                                    **thread, "config": config}
            kw["extra_env"] = {**CHAT_ENV, **(kw.get("extra_env") or {})}
            # the capture would keep every request (images included) in the workspace
            kw.setdefault("capture", False)
        if effort and self.transport == "sdk":
            kw["turn_options"] = {"effort": effort, **(kw.get("turn_options") or {})}
        return kw

    def session(self, **kwargs: Unpack[ModelSessionOptions]) -> SDKSession | Session:
        """Create a session for the selected transport, inheriting model settings."""
        if self.transport == "sdk":
            return self.sdk_session(**cast(SDKSessionOptions, kwargs))
        options: ModelSessionOptions = {
            "model": self.model, "workspace": self.workspace, "timeout": self.timeout,
            "mcp_servers": self.mcp_servers, "codex_home": self.codex_home,
        }
        if self.api_key:
            options["extra_env"] = {"OPENAI_API_KEY": self.api_key}
        options.update(kwargs)
        return Session(**cast(SessionOptions, options))

    def sdk_session(self, **kwargs: Unpack[SDKSessionOptions]) -> SDKSession:
        """Create a persistent SDK thread with this model's capture and auth settings."""
        options = self._call_kwargs({"prompt": ""})
        if self.transport == "exec":
            options.update(self.sdk_options)
        options.pop("turn_options", None)
        options.update(kwargs)
        return SDKSession(**cast(SDKSessionOptions, options))
