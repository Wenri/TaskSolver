"""Request schemas and response accessors for HTTP model adapters."""
from typing import Literal, TypedDict

from anthropic.types import MessageParam
from openai.types.chat import ChatCompletionMessageParam

from wirecap.types import JSONObject, JSONValue
from .common import ParsedAnswer, Question, TaskSpec
from .exceptions import GPTOutputParseException


class OpenAIPayload(TypedDict):
    model: str
    messages: list[ChatCompletionMessageParam]
    max_tokens: int


class AnthropicPayload(TypedDict):
    messages: MessageParam
    max_tokens: int


type BackendResult = tuple[ParsedAnswer, object, object, object]
type BackendManyResults = tuple[list[ParsedAnswer], object, object, object]


def response_text(message: JSONObject) -> str:
    content = message.get("content")
    if not isinstance(content, str):
        raise GPTOutputParseException("the model returned no text content")
    return content


def parse_response(task: TaskSpec, message: JSONObject) -> ParsedAnswer:
    return task.answer_type.parser(response_text(message))


def object_list(value: JSONValue) -> list[JSONObject]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("expected a list of response objects")
    if not all(isinstance(item, dict) for item in value):
        raise TypeError("expected response objects")
    return [item for item in value if isinstance(item, dict)]


def openai_messages(question: Question, prepend: ChatCompletionMessageParam | None) -> list[ChatCompletionMessageParam]:
    from openai.types.chat import ChatCompletionContentPartParam
    parts: list[ChatCompletionContentPartParam] = []
    for part in question.get_json():
        if part["type"] == "text":
            parts.append({"type": "text", "text": part["text"]})
        else:
            parts.append({"type": "image_url", "image_url": {"url": part["image_url"]["url"]}})
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": parts}]
    if prepend is not None:
        messages.insert(0, prepend)
    return messages


class ThreadResult(TypedDict):
    message: JSONObject
    metadata: JSONObject


def completed_results(results: list[ThreadResult | None]) -> list[ThreadResult]:
    if any(result is None for result in results):
        raise RuntimeError("a model request failed before producing a response")
    return [result for result in results if result is not None]


def media_type(image_format: Literal["jpeg", "png", "gif", "webp"]) -> Literal["image/jpeg", "image/png", "image/gif", "image/webp"]:
    match image_format:
        case "jpeg": return "image/jpeg"
        case "png": return "image/png"
        case "gif": return "image/gif"
        case "webp": return "image/webp"
