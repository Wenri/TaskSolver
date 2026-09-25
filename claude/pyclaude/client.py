"""Structured Claude Agent SDK calls without wirecap or a separately installed CLI.

Every turn owns the full async SDK lifecycle. Session follow-ups resume the saved
Claude session in a new SDK client, avoiding cross-task AnyIO lifecycle ownership.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Final


def _load_sdk():
    try:
        return importlib.import_module("claude_agent_sdk")
    except ImportError as exc:
        raise RuntimeError(
            "TaskSolver's vendored Claude Agent SDK is unavailable. "
            "Run `pixi install` from the TaskSolver or host workspace."
        ) from exc


def _plain(value: Any) -> Any:
    """Keep SDK messages and content blocks as serializable native metadata."""
    if is_dataclass(value) and not isinstance(value, type):
        data: Final = {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
        name: Final = type(value).__name__.removesuffix("Message").removesuffix("Block")
        data.setdefault("type", re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower())
        return data
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@dataclass
class ClaudeResponse:
    """One completed SDK turn, retaining native usage and terminal metadata."""

    text: str = ""
    thoughts: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    total_cost_usd: float | None = None
    workspace: str | None = None
    request: dict[str, Any] = field(default_factory=dict)
    timed_out: bool = False

    @property
    def transcript(self) -> str:
        return "\n".join(json.dumps(message, ensure_ascii=False) for message in self.messages)

    @property
    def exit_status(self) -> int:
        return int(self.timed_out or not self.result or bool(self.result.get("is_error"))
                   or self.result.get("subtype", "success") != "success")

    def metadata(self) -> dict[str, Any]:
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

    def __init__(self, message: str, response: ClaudeResponse):
        super().__init__(message)
        self.response = response


def _tool_names(value: str | list[str] | tuple[str, ...] | None):
    if isinstance(value, str):
        return [part for part in re.split(r"[\s,]+", value) if part]
    return list(value) if value is not None else None


def _options(sdk, *, model, workspace, api_key, extra_env, mcp_servers,
             session_id, tools, allowed_tools, permission_mode, sdk_options):
    options: Final = dict(sdk_options or {})
    env: Final = dict(options.get("env") or {})
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
        servers: Final = {}
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
    prompt: str | list[dict[str, Any]], *, model: str | None = None,
    workspace: str | os.PathLike | None = None, timeout: float | None = 300,
    api_key: str | None = None, extra_env: dict[str, str] | None = None,
    mcp_servers: dict[str, Any] | None = None, session_id: str | None = None,
    tools: str | list[str] | tuple[str, ...] | None = ("Read",),
    allowed_tools: str | list[str] | tuple[str, ...] | None = ("Read",),
    permission_mode: str = "default", sdk_options: dict[str, Any] | None = None,
) -> ClaudeResponse:
    """Run a text or Anthropic content-block prompt using the official SDK.

    ``session_id`` resumes an existing conversation. ``tools`` limits built-in
    tools; MCP tools remain available, with approval controlled by
    ``allowed_tools`` and ``permission_mode``. Dedicated arguments override
    matching ``sdk_options`` fields; other SDK fields pass through unchanged.
    """
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive or None")
    if not isinstance(prompt, (str, list)):
        raise TypeError("prompt must be a string or a list of SDK content blocks")
    sdk: Final = _load_sdk()
    options: Final = _options(
        sdk, model=model, workspace=workspace, api_key=api_key, extra_env=extra_env,
        mcp_servers=mcp_servers, session_id=session_id, tools=tools,
        allowed_tools=allowed_tools, permission_mode=permission_mode, sdk_options=sdk_options,
    )
    response: Final = ClaudeResponse(
        model=model, workspace=os.fspath(workspace) if workspace is not None else None,
        request={"prompt": copy.deepcopy(prompt), "model": model},
    )

    async def user_message():
        yield {"type": "user", "message": {"role": "user", "content": copy.deepcopy(prompt)},
               "parent_tool_use_id": None}

    async def run_turn():
        terminal = None
        last_text = ""
        async with sdk.ClaudeSDKClient(options=options) as client:
            await client.query(prompt if isinstance(prompt, str) else user_message())
            async for message in client.receive_response():
                response.messages.append(_plain(message))
                if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                    response.session_id = message.data.get("session_id") or response.session_id
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
                    response.result = _plain(message)
                    response.session_id = message.session_id
                    response.usage = dict(message.usage or {})
                    response.total_cost_usd = message.total_cost_usd
                    response.text = message.result if message.result is not None else last_text
        if terminal is None:
            response.text = last_text
            raise ClaudeAgentError("Claude Agent SDK ended without a terminal result", response)
        if terminal.is_error or terminal.subtype != "success":
            details: Final = response.result.get("errors") or [response.text or terminal.subtype]
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
        setattr(exc, "response", response)
        raise


def _run_sync(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # Notebook/UI callers may already own an event loop in this thread.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyclaude") as executor:
        return executor.submit(asyncio.run, coroutine).result()


def ask(prompt: str | list[dict[str, Any]], **kwargs) -> ClaudeResponse:
    """Synchronous ``ask_async``, also usable inside an existing event loop."""
    return _run_sync(ask_async(prompt, **kwargs))


def ask_many(prompt: str | list[dict[str, Any]], n: int, *, max_workers: int | None = None,
             **kwargs) -> list[ClaudeResponse]:
    """Sample independent conversations concurrently, preserving input order."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
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
    requested through this object, including a failed terminal response. Calls
    on the same Session must not overlap; separate Sessions can run concurrently.
    """

    def __init__(self, *, session_id: str | None = None, **kwargs):
        self.session_id = session_id
        self._kwargs = kwargs
        self._history: list[ClaudeResponse] = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def conversation_id(self) -> str | None:
        return self.session_id

    async def ask_async(self, prompt: str | list[dict[str, Any]], **kwargs) -> ClaudeResponse:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Session already has a turn in progress")
        try:
            if self._closed:
                raise RuntimeError("Session is closed")
            options: Final = {**self._kwargs, **kwargs, "session_id": self.session_id}
            try:
                response = await ask_async(prompt, **options)
            except (ClaudeAgentError, TimeoutError) as exc:
                partial: Final = getattr(exc, "response", None)
                if partial is not None:
                    self._remember(partial)
                raise
            self._remember(response)
            return response
        finally:
            self._lock.release()

    def ask(self, prompt: str | list[dict[str, Any]], **kwargs) -> ClaudeResponse:
        return _run_sync(self.ask_async(prompt, **kwargs))

    def _remember(self, response: ClaudeResponse) -> None:
        if response.session_id:
            self.session_id = response.session_id
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

    def __enter__(self):
        if self._closed:
            raise RuntimeError("Session is closed")
        return self

    def __exit__(self, *_):
        self.close()

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *_):
        self.close()
