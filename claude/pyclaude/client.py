"""Structured Claude Agent SDK calls using the bundled SDK and CLI.

Every turn owns the full async SDK lifecycle. Session follow-ups resume the saved
Claude session in a new SDK client, avoiding cross-task AnyIO lifecycle ownership.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields, is_dataclass
from collections.abc import AsyncIterator, Coroutine, Mapping
from typing import Final, Self, Unpack, cast

from wirecap.types import JSONObject, JSONValue, json_object
from .types import AskOptions, MCPServers, Prompt, SDK, SDKOptionsFactory, SDKResultMessage, Tools


def _load_sdk() -> SDK:
    try:
        return cast(SDK, importlib.import_module("claude_agent_sdk"))
    except ImportError as exc:
        raise RuntimeError(
            "TaskSolver's vendored Claude Agent SDK is unavailable. "
            "Run `pixi install` from the TaskSolver or host workspace."
        ) from exc


def _plain(value: object) -> JSONValue:
    """Keep SDK messages and content blocks as serializable native metadata."""
    if is_dataclass(value) and not isinstance(value, type):
        data: JSONObject = {item.name: _plain(cast(object, getattr(value, item.name))) for item in fields(value)}
        name: Final = type(value).__name__.removesuffix("Message").removesuffix("Block")
        data.setdefault("type", re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower())
        return data
    if isinstance(value, dict):
        values = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in values):
            raise TypeError("SDK metadata keys must be strings")
        return {str(key): _plain(item) for key, item in values.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in cast(list[object] | tuple[object, ...], value)]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"SDK metadata cannot serialize {type(value).__name__}")


@dataclass
class ClaudeResponse:
    """A captured SDK turn, including partial results and native metadata."""

    text: str = ""
    thoughts: list[str] = field(default_factory=list)
    messages: list[JSONObject] = field(default_factory=list)
    result: JSONObject = field(default_factory=dict)
    session_id: str | None = None
    model: str | None = None
    usage: JSONObject = field(default_factory=dict)
    total_cost_usd: float | None = None
    workspace: str | None = None
    request: JSONObject = field(default_factory=dict)
    timed_out: bool = False

    @property
    def transcript(self) -> str:
        return "\n".join(json.dumps(message, ensure_ascii=False) for message in self.messages)

    @property
    def exit_status(self) -> int:
        return int(self.timed_out or not self.result or bool(self.result.get("is_error"))
                   or self.result.get("subtype", "success") != "success")

    def metadata(self) -> dict[str, object]:
        return {
            **self.result,
            "result": self.text,
            "session_id": self.session_id,
            "model": self.model,
            "usage": self.usage,
            "total_cost_usd": self.total_cost_usd,
            "workspace": self.workspace,
            "timed_out": self.timed_out,
            "exit_status": self.exit_status,
            "messages": self.messages,
            "thinking": self.thoughts,
            "explicit_reasoning_output": "\n\n".join(self.thoughts) or None,
        }

    def __str__(self) -> str:
        return self.text


class ClaudeAgentError(RuntimeError):
    """A failed or incomplete SDK turn; ``response`` retains its diagnostics."""

    def __init__(self, message: str, response: ClaudeResponse) -> None:
        super().__init__(message)
        self.response = response


class ClaudeAgentTimeout(TimeoutError):
    """A timed-out SDK turn with its partial response."""

    def __init__(self, *args: object, response: ClaudeResponse) -> None:
        super().__init__(*args)
        self.response = response


def _tool_names(value: Tools) -> list[str] | None:
    if isinstance(value, str):
        return [part for part in re.split(r"[\s,]+", value) if part]
    return list(value) if value is not None else None


#: Variables a running Claude Code session exports to its tool subprocesses. An SDK child is
#: a separate session, but the SDK passes the parent environment through (dropping only
#: CLAUDECODE), so a child launched from inside Claude Code would join the parent's session id,
#: messaging socket and effort. ``ClaudeAgentOptions.env`` can override but not unset a
#: variable, so they are blanked; the CLI treats an empty value as unset.
PARENT_SESSION_ENV: Final = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "CLAUDE_EFFORT",
)


def _options[OptionsT](sdk: SDKOptionsFactory[OptionsT], *, model: str | None, workspace: str | os.PathLike[str] | None,
             api_key: str | None, extra_env: dict[str, str] | None, mcp_servers: MCPServers | None,
             session_id: str | None, tools: Tools, allowed_tools: Tools,
             permission_mode: str, sdk_options: dict[str, object] | None) -> OptionsT:
    options: dict[str, object] = dict(sdk_options or {})
    env: Final = {name: "" for name in PARENT_SESSION_ENV if os.environ.get(name)}
    configured_env = options.get("env")
    if configured_env is not None:
        if not isinstance(configured_env, Mapping):
            raise TypeError("SDK env must map strings to strings")
        for name, value in cast(Mapping[object, object], configured_env).items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError("SDK env must map strings to strings")
            env[name] = value
    env.update(extra_env or {})
    if api_key is not None:
        env["ANTHROPIC_API_KEY"] = api_key
    options.update(env=env, tools=_tool_names(tools),
                   allowed_tools=_tool_names(allowed_tools) or [],
                   permission_mode=permission_mode)
    if model is not None:
        options["model"] = model
    if workspace is not None:
        options["cwd"] = os.fspath(workspace)
    if mcp_servers is not None:
        from wirecap.runtime.mcp import env_wrapped, normalize_server_spec

        # SDK in-process servers contain live objects that cannot be deep-copied.
        servers: dict[str, Mapping[str, object]] = {}
        for name, spec in mcp_servers.items():
            if spec.get("type", "stdio") == "stdio":
                servers[name] = (env_wrapped(spec) if os.name == "posix"
                                 else normalize_server_spec(spec))
            else:
                servers[name] = dict(spec)
        options["mcp_servers"] = servers
        options["strict_mcp_config"] = True
    if session_id is not None:
        if options.get("session_id") or options.get("continue_conversation"):
            raise ValueError("session_id resume cannot be combined with a new/latest session")
        options["resume"] = session_id
    return sdk.ClaudeAgentOptions(**options)


async def ask_async(
    prompt: Prompt, *, model: str | None = None,
    workspace: str | os.PathLike[str] | None = None, timeout: float | None = 300,
    api_key: str | None = None, extra_env: dict[str, str] | None = None,
    mcp_servers: MCPServers | None = None, session_id: str | None = None,
    tools: str | list[str] | tuple[str, ...] | None = ("Read",),
    allowed_tools: str | list[str] | tuple[str, ...] | None = ("Read",),
    permission_mode: str = "default", sdk_options: dict[str, object] | None = None,
) -> ClaudeResponse:
    """Run a text or Anthropic content-block prompt using the official SDK.

    ``session_id`` resumes an existing conversation. ``tools`` limits built-in
    tools; MCP tools remain available, with approval controlled by
    ``allowed_tools`` and ``permission_mode``. Dedicated arguments override
    matching ``sdk_options`` fields; other SDK fields pass through unchanged.
    """
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive or None")
    sdk: Final = _load_sdk()
    options: Final = _options(
        sdk, model=model, workspace=workspace, api_key=api_key, extra_env=extra_env,
        mcp_servers=mcp_servers, session_id=session_id, tools=tools,
        allowed_tools=allowed_tools, permission_mode=permission_mode, sdk_options=sdk_options,
    )
    response: Final = ClaudeResponse(
        model=model, workspace=os.fspath(workspace) if workspace is not None else None,
        request={"prompt": _plain(prompt), "model": model},
    )

    async def user_message() -> AsyncIterator[JSONObject]:
        yield {"type": "user", "message": {"role": "user", "content": _plain(prompt)},
               "parent_tool_use_id": None}

    async def run_turn() -> ClaudeResponse:
        terminal: SDKResultMessage | None = None
        last_text = ""
        async with sdk.ClaudeSDKClient(options=options) as client:
            await client.query(prompt if isinstance(prompt, str) else user_message())
            async for message in client.receive_response():
                response.messages.append(json_object(_plain(message)))
                if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                    session_id = message.data.get("session_id")
                    if isinstance(session_id, str) and session_id:
                        response.session_id = session_id
                elif isinstance(message, sdk.AssistantMessage):
                    if message.parent_tool_use_id is not None:
                        continue
                    response.model = message.model or response.model
                    last_text = "\n".join(block.text for block in message.content
                                          if isinstance(block, sdk.TextBlock))
                    response.text = last_text
                    response.thoughts.extend(block.thinking for block in message.content
                                             if isinstance(block, sdk.ThinkingBlock))
                elif isinstance(message, sdk.ResultMessage):
                    terminal = message
                    response.result = json_object(_plain(message))
                    response.session_id = message.session_id
                    response.usage = dict(message.usage or {})
                    response.total_cost_usd = message.total_cost_usd
                    response.text = message.result if message.result is not None else last_text
        if terminal is None:
            response.text = last_text
            raise ClaudeAgentError("Claude Agent SDK ended without a terminal result", response)
        if terminal.is_error or terminal.subtype != "success":
            details = response.result.get("errors") or [response.text or terminal.subtype]
            details = details if isinstance(details, list) else [details]
            raise ClaudeAgentError("Claude Agent SDK failed: " + "; ".join(map(str, details)),
                                   response)
        return response

    # Use AnyIO cancellation: the SDK's shutdown shield does not protect cleanup
    # from raw asyncio.wait_for cancellation. This scope owns the full lifecycle.
    import anyio

    try:
        with anyio.fail_after(timeout):
            return await run_turn()
    except TimeoutError as exc:
        response.timed_out = True
        raise ClaudeAgentTimeout(*cast(tuple[object, ...], exc.args), response=response) from exc
    except BaseException as exc:
        # A transport error or external cancellation can arrive after init.
        # Preserve that session without replacing the exception or its own response.
        setattr(exc, "_claude_response", response)
        raise


def _run_sync[T](coroutine: Coroutine[object, object, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # Notebook/UI callers may already own an event loop in this thread.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyclaude") as executor:
        return executor.submit(asyncio.run, coroutine).result()


def ask(prompt: Prompt, **kwargs: Unpack[AskOptions]) -> ClaudeResponse:
    """Synchronous ``ask_async``, also usable inside an existing event loop."""
    return _run_sync(ask_async(prompt, **kwargs))


def ask_many(prompt: Prompt, n: int, *, max_workers: int | None = None,
             **kwargs: Unpack[AskOptions]) -> list[ClaudeResponse]:
    """Sample independent conversations concurrently, preserving input order."""
    if isinstance(n, bool) or n < 1:
        raise ValueError("n must be a positive integer")
    if n > 1 and (kwargs.get("session_id") or
                  any((kwargs.get("sdk_options") or {}).get(name)
                      for name in ("resume", "session_id", "continue_conversation"))):
        raise ValueError("parallel samples cannot write to the same resumed session")
    with ThreadPoolExecutor(max_workers=max_workers or min(n, 32),
                            thread_name_prefix="pyclaude-sample") as executor:
        futures: Final = [executor.submit(ask, prompt, **kwargs) for _ in range(n)]
        return [future.result() for future in futures]


class Session:
    """Sequential turns resumed by ID; a fresh SDK client is opened per turn.

    The SDK stores the transcript locally. ``history()`` contains only responses
    requested through this object, including failed or interrupted turns. Calls
    on the same Session must not overlap; separate Sessions can run concurrently.
    """

    def __init__(self, **kwargs: Unpack[AskOptions]) -> None:
        session_id = kwargs.pop("session_id", None)
        self.session_id = session_id
        self._kwargs = kwargs
        self._history: list[ClaudeResponse] = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def conversation_id(self) -> str | None:
        return self.session_id

    async def ask_async(self, prompt: Prompt, **kwargs: Unpack[AskOptions]) -> ClaudeResponse:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Session already has a turn in progress")
        try:
            if self._closed:
                raise RuntimeError("Session is closed")
            options: AskOptions = {**self._kwargs, **kwargs, "session_id": self.session_id}
            try:
                response = await ask_async(prompt, **options)
            except BaseException as exc:
                partial = (exc.response if isinstance(exc, (ClaudeAgentError, ClaudeAgentTimeout))
                           else cast(object, getattr(exc, "_claude_response", None)))
                if isinstance(partial, ClaudeResponse):
                    self._remember(partial)
                raise
            self._remember(response)
            return response
        finally:
            self._lock.release()

    def ask(self, prompt: Prompt, **kwargs: Unpack[AskOptions]) -> ClaudeResponse:
        return _run_sync(self.ask_async(prompt, **kwargs))

    def _remember(self, response: ClaudeResponse) -> None:
        if response.session_id:
            self.session_id = response.session_id
            # New/latest flags only select the first turn; later turns use its ID.
            options = self._kwargs.get("sdk_options")
            if options and ("session_id" in options or "continue_conversation" in options):
                self._kwargs["sdk_options"] = {
                    key: value for key, value in options.items()
                    if key not in ("session_id", "continue_conversation")
                }
        self._history.append(response)

    def history(self) -> list[ClaudeResponse]:
        return list(self._history)

    def close(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Cannot close a Session while a turn is in progress")
        try:
            self._closed = True
        finally:
            self._lock.release()

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("Session is closed")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self.__enter__()

    async def __aexit__(self, *_: object) -> None:
        self.close()
