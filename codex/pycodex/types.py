"""Keyword contracts for the pinned Codex SDK wrapper."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from os import PathLike
from typing import TYPE_CHECKING, TypedDict

from wirecap.runtime.mcp import MCPServers
from wirecap.types import JSONObject

if TYPE_CHECKING:
    from openai_codex import ApprovalMode, Sandbox
    from openai_codex.types import Personality, ReasoningEffort, ReasoningSummary, ThreadSource, ThreadStartSource


class CommonThreadOptions(TypedDict, total=False):
    base_instructions: str | None
    config: JSONObject | None
    cwd: str | None
    developer_instructions: str | None
    model: str | None
    model_provider: str | None
    personality: Personality | None
    sandbox: Sandbox | None
    service_tier: str | None


class StartThreadOptions(CommonThreadOptions, total=False):
    approval_mode: ApprovalMode
    ephemeral: bool | None
    service_name: str | None
    session_start_source: ThreadStartSource | None
    thread_source: ThreadSource | None


class ResumeThreadOptions(CommonThreadOptions, total=False):
    approval_mode: ApprovalMode | None
    include_turns: bool | None


class ThreadOptions(CommonThreadOptions, total=False):
    """The selected SDK start/resume method validates its supported keys."""
    approval_mode: ApprovalMode | None
    ephemeral: bool | None
    service_name: str | None
    session_start_source: ThreadStartSource | None
    thread_source: ThreadSource | None
    include_turns: bool | None


class CommonTurnOptions(TypedDict, total=False):
    approval_mode: ApprovalMode | None
    cwd: str | None
    model: str | None
    output_schema: JSONObject | None
    personality: Personality | None
    sandbox: Sandbox | None
    service_tier: str | None
    source: str | None
    summary: ReasoningSummary | None
    turn_service_tier: str | None


class TurnOptions(CommonTurnOptions, total=False):
    effort: ReasoningEffort | str | None


class SDKTurnOptions(CommonTurnOptions, total=False):
    effort: ReasoningEffort | None


class BaseSDKSessionOptions(TypedDict, total=False):
    model: str | None
    workspace: str | PathLike[str] | None
    timeout: float
    codex_bin: str | PathLike[str] | None
    extra_env: Mapping[str, str] | None
    mcp_servers: MCPServers | None
    codex_home: str | PathLike[str] | None
    session_id: str | None
    continue_latest: bool
    capture: bool | str | PathLike[str]
    config_overrides: Sequence[str]
    thread_options: ThreadOptions | None


class SDKSessionOptions(BaseSDKSessionOptions, total=False, closed=True):
    pass


class SDKAskOptions(BaseSDKSessionOptions, total=False, closed=True):
    turn_options: TurnOptions | None


class ModelSessionOptions(BaseSDKSessionOptions, total=False, closed=True):
    extra_flags: Sequence[str] | None
    idle: float
    capture_tail: bool
