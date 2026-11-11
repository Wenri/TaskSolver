"""Typed boundaries for the lazily loaded Claude SDK."""
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from os import PathLike
from typing import Protocol, runtime_checkable
from typing_extensions import TypedDict

from wirecap.types import JSONObject


type Prompt = str | list[JSONObject]
type Tools = str | list[str] | tuple[str, ...] | None
type MCPServers = Mapping[str, Mapping[str, object]]


class AskOptions(TypedDict, total=False, closed=True):
    model: str | None
    workspace: str | PathLike[str] | None
    timeout: float | None
    api_key: str | None
    extra_env: dict[str, str] | None
    mcp_servers: MCPServers | None
    session_id: str | None
    tools: Tools
    allowed_tools: Tools
    permission_mode: str
    sdk_options: dict[str, object] | None


@runtime_checkable
class SDKSystemMessage(Protocol):
    subtype: str
    data: JSONObject


@runtime_checkable
class SDKAssistantMessage(Protocol):
    parent_tool_use_id: str | None
    model: str
    content: Sequence[object]


@runtime_checkable
class SDKResultMessage(Protocol):
    session_id: str
    usage: JSONObject | None
    total_cost_usd: float | None
    result: str | None
    is_error: bool
    subtype: str


@runtime_checkable
class SDKTextBlock(Protocol):
    text: str


@runtime_checkable
class SDKThinkingBlock(Protocol):
    thinking: str


class SDKClient(Protocol):
    async def query(self, prompt: str | AsyncIterator[JSONObject]) -> None: ...
    def receive_response(self) -> AsyncIterator[object]: ...


class TerminalSDKClient(Protocol):
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> AsyncIterator[object]: ...
    async def interrupt(self) -> None: ...
    async def get_server_info(self) -> JSONObject | None: ...


class SDKOptionsFactory[OptionsT](Protocol):
    def ClaudeAgentOptions(self, **options: object) -> OptionsT: ...


class TerminalSDK(SDKOptionsFactory[object], Protocol):
    def ClaudeSDKClient(self, *, options: object,
                        transport: object) -> AbstractAsyncContextManager[TerminalSDKClient]: ...


class SDK(SDKOptionsFactory[object], Protocol):
    SystemMessage: type[SDKSystemMessage]
    AssistantMessage: type[SDKAssistantMessage]
    ResultMessage: type[SDKResultMessage]
    TextBlock: type[SDKTextBlock]
    ThinkingBlock: type[SDKThinkingBlock]
    def ClaudeSDKClient(self, *, options: object) -> AbstractAsyncContextManager[SDKClient]: ...
