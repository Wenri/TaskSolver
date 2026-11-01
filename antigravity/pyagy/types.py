"""Typed declarations shared by the agy client and its stdlib-only tools."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from wirecap.types import JSONObject

type ToolHandler = Callable[[JSONObject], object]
type RewriteFunction = Callable[[bytes], bytes | None]


class RewriteSpec(TypedDict):
    find: str
    replace: str
    count: NotRequired[int]
    regex: NotRequired[bool]


@dataclass
class ToolSpec:
    """A custom MCP tool backed by an importable handler or static response."""
    name: str
    description: str = ""
    input_schema: JSONObject | None = None
    handler: str | ToolHandler | None = None
    response: str = ""


class ToolInput(TypedDict):
    name: str
    description: NotRequired[str]
    input_schema: NotRequired[JSONObject]
    inputSchema: NotRequired[JSONObject]
    handler: NotRequired[str | ToolHandler | None]
    response: NotRequired[str]


@dataclass
class ContextResource:
    """An inline text resource exposed through MCP."""
    uri: str
    text: str = ""
    mime_type: str = "text/plain"
    name: str | None = None


class ContextInput(TypedDict):
    uri: str
    text: NotRequired[str]
    mime_type: NotRequired[str]
    mimeType: NotRequired[str]
    name: NotRequired[str | None]


@dataclass
class RewriteRule:
    """An equal-length UTF-8 egress substitution; count zero replaces every match."""
    pattern: str
    replacement: str
    count: int = 0
    regex: bool = False

    def as_dict(self) -> RewriteSpec:
        return {"find": self.pattern, "replace": self.replacement,
                "count": self.count, "regex": self.regex}
