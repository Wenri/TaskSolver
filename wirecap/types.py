"""Shared JSON types and readers; safe to import in embedded CLI interpreters."""
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol, TextIO, cast
import json

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]
type JSONMapping = Mapping[str, JSONValue]
type JSONArray = Sequence[JSONValue]
type Event = dict[str, object]
type StreamId = int | str | None
type Direction = Literal["c2s", "s2c"]


class EventSink(Protocol):
    def event(self, obj: Mapping[str, object]) -> None: ...


def json_loads(data: str | bytes | bytearray) -> JSONValue:
    """Decode JSON without propagating the stdlib decoder's dynamic return type."""
    return cast(JSONValue, json.loads(data))


def json_load(stream: TextIO) -> JSONValue:
    return json_loads(stream.read())


def json_object(value: JSONValue) -> JSONObject:
    """Require a JSON object at a protocol boundary."""
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    return value


def json_array(value: JSONValue) -> list[JSONValue]:
    if not isinstance(value, list):
        raise TypeError("expected a JSON array")
    return value


def json_loads_object(data: str | bytes | bytearray) -> JSONObject:
    return json_object(json_loads(data))


def json_load_object(stream: TextIO) -> JSONObject:
    return json_object(json_load(stream))
