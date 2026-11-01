from __future__ import annotations

from wirecap.types import JSONObject, JSONValue
from .provider_types import BackendResult, BackendManyResults, response_text
from openai.types.chat import ChatCompletionMessageParam
from .provider_types import OpenAIPayload, openai_messages, object_list
import os
from urllib.parse import urlparse
from typing import TypedDict, cast

from loguru import logger
from openai import OpenAI

from .common import ParsedAnswer, Question, TaskSpec, attach_response_metadata, extract_explicit_reasoning_output
from .exceptions import GPTMaxTriesExceededException, GPTOutputParseException


DEFAULT_QWEN3_MODEL = "Qwen/Qwen3.5-397B-A17B-GPTQ-Int4"
MIN_QWEN3_MAX_TOKENS = 10000
QWEN3_BUILTIN_ENDPOINTS = {
    "qwen3-5": {
        "base_url": "https://vlm1.wenri.me/v1",
        "model": "Qwen/Qwen3.5-397B-A17B-GPTQ-Int4",
    },
    "qwen3-6": {
        "base_url": "https://vlm2.wenri.me/v1",
        "model": "Qwen/Qwen3.6-27B-FP8",
    },
}
QWEN3_BASE_URL_MODEL_ALIASES = {
    "vlm1.wenri.me": "Qwen/Qwen3.5-397B-A17B-GPTQ-Int4",
    "vega-lix.polytechnique.fr": "Qwen/Qwen3.5-397B-A17B-GPTQ-Int4",
    "vlm2.wenri.me": "Qwen/Qwen3.6-27B-FP8",
    "saturn-lix.polytechnique.fr": "Qwen/Qwen3.6-27B-FP8",
}
QWEN3_BASE_URL_ENV_VARS = (
    "QWEN3_OPENAI_BASE_URL",
    "QWEN3_BASE_URL",
    "VLLM_OPENAI_BASE_URL",
    "VLLM_BASE_URL",
)
QWEN3_MODEL_ENV_VARS = (
    "QWEN3_MODEL_NAME",
    "QWEN3_MODEL",
    "VLLM_MODEL_NAME",
    "VLLM_MODEL",
)


class ChoiceState(TypedDict):
    message: JSONObject
    finish_reason: JSONValue


class VLLMModel(object):
    def __init__(
        self,
        api_key: str | None,
        task: TaskSpec,
        model: str = DEFAULT_QWEN3_MODEL,
        base_url: str | None = None,
    ):
        self.api_key = api_key
        self.task = task
        self.model = model
        self.base_url = base_url
        self.thinking_depth = None

    def _normalize_max_tokens(self, max_tokens: int) -> int:
        return max(max_tokens, MIN_QWEN3_MAX_TOKENS)

    def _default_extra_body(self) -> JSONObject:
        return {
            "chat_template_kwargs": {
                "enable_thinking": self.thinking_depth is not None,
            },
        }

    @staticmethod
    def _extract_message_text(message: JSONObject) -> JSONValue:
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                text for part in object_list(content)
                if isinstance(text := part.get("text"), str)
            )

        if content:
            return content

        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, list):
            reasoning = "".join(
                text for part in object_list(reasoning)
                if isinstance(text := part.get("text"), str)
            )

        if isinstance(reasoning, str) and "```python" in reasoning:
            return reasoning

        return content

    @staticmethod
    def _summarize_empty_response(message: JSONObject, model: str, max_tokens: int) -> str:
        finish_reason = message.get("finish_reason")
        has_reasoning = bool(message.get("reasoning_content"))
        return (
            "The vLLM endpoint returned an empty visible response "
            f"for model={model}. Requested max_tokens={max_tokens}. "
            f"finish_reason={finish_reason!r}, reasoning_content_present={has_reasoning}."
        )

    def ask(self, payload: OpenAIPayload, n_choices: int = 1) -> tuple[list[JSONObject], JSONValue]:
        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=180.0,
        )

        stream = client.chat.completions.create(
            model=self.model,
            messages=payload["messages"],
            max_tokens=payload["max_tokens"],
            n=n_choices,
            extra_body=self._default_extra_body(),
            stream=True,
            stream_options={"include_usage": True},
        )

        choice_state: dict[int, ChoiceState] = {
            i: {
                "message": {
                    "role": "assistant",
                    "content": "",
                },
                "finish_reason": None,
            }
            for i in range(n_choices)
        }
        usage: JSONValue = None

        for chunk in stream:
            chunk_dict = cast(JSONObject, chunk.model_dump())

            if chunk_dict.get("usage"):
                usage = chunk_dict["usage"]

            for choice in object_list(chunk_dict["choices"]):
                index = choice["index"]
                if not isinstance(index, int):
                    raise TypeError("choice index must be an integer")
                delta = choice["delta"]
                if not isinstance(delta, dict):
                    raise TypeError("choice delta must be an object")

                if delta.get("role"):
                    choice_state[index]["message"]["role"] = delta["role"]

                if delta.get("content"):
                    text = delta["content"]
                    if not isinstance(text, str):
                        raise TypeError("content delta must be text")
                    message = choice_state[index]["message"]
                    message["content"] = response_text(message) + text

                if delta.get("reasoning_content"):
                    message = choice_state[index]["message"]
                    previous = message.get("reasoning_content", "")
                    addition = delta["reasoning_content"]
                    if not isinstance(previous, str) or not isinstance(addition, str):
                        raise TypeError("reasoning deltas must be text")
                    message["reasoning_content"] = previous + addition

                if choice.get("finish_reason"):
                    choice_state[index]["finish_reason"] = choice["finish_reason"]

        messages: list[JSONObject] = []
        for index in range(n_choices):
            state = choice_state[index]
            message = state["message"]
            message["finish_reason"] = state["finish_reason"]
            message["explicit_reasoning_output"] = extract_explicit_reasoning_output(message)
            message["content"] = self._extract_message_text(message)
            messages.append(message)
        return messages, usage

    @staticmethod
    def prepare_payload(question: Question, verbose: bool = False,
                        prepend: ChatCompletionMessageParam | None = None,
                        model: str = DEFAULT_QWEN3_MODEL, max_tokens: int = 4096) -> OpenAIPayload:
        return {"model": model, "messages": openai_messages(question, prepend), "max_tokens": max_tokens}

    def many_rough_guesses(
        self,
        num_threads: int,
        question: Question,
        max_tokens: int = 4096,
        verbose: bool = False,
        max_tries: int = 1,
    ) -> BackendManyResults:
        p = self.prepare_payload(
            question,
            verbose=verbose,
            prepend=None,
            model=self.model,
            max_tokens=self._normalize_max_tokens(max_tokens),
        )

        reattempt = 0
        while True:
            response, meta_data = self.ask(p, n_choices=num_threads)
            try:
                parsed_response: list[ParsedAnswer] = []
                for message in response:
                    content = message.get("content")
                    if not isinstance(content, str):
                        raise GPTOutputParseException(self._summarize_empty_response(
                            message,
                            self.model,
                            p["max_tokens"],
                        ))
                    parsed_response.append(
                        attach_response_metadata(
                            self.task.answer_type.parser(content),
                            response_metadata=message,
                            request_payload=p,
                        )
                    )
            except GPTOutputParseException:
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p

    def rough_guess(
        self,
        question: Question,
        max_tokens: int = 4096,
        verbose: bool = False,
        max_tries: int = 1,
        query_id: int = 0,
    ) -> BackendResult:
        p = self.prepare_payload(
            question,
            verbose=verbose,
            prepend=None,
            model=self.model,
            max_tokens=self._normalize_max_tokens(max_tokens),
        )

        reattempt = 0
        while True:
            responses, meta_data = self.ask(p)
            response = responses[0]
            try:
                content = response.get("content")
                if not isinstance(content, str):
                    raise GPTOutputParseException(self._summarize_empty_response(
                        response,
                        self.model,
                        p["max_tokens"],
                    ))
                parsed_response = attach_response_metadata(
                    self.task.answer_type.parser(content),
                    response_metadata=response,
                    request_payload=p,
                )
            except GPTOutputParseException:
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            break
        return parsed_response, response, meta_data, p

    def run_once(self, question: Question, max_tokens: int = 4096, *, max_tries: int = 1, query_id: int = 0, verbose: bool = False, **kwargs: object) -> BackendResult:
        q = self.task.first_question(question)
        return self.rough_guess(q, max_tokens=max_tokens, max_tries=max_tries, query_id=query_id, verbose=verbose)


def resolve_qwen3_api_key(explicit_api_key: str | None = None) -> str | None:
    if explicit_api_key:
        if (
            not os.path.exists(explicit_api_key)
            and ("/" in explicit_api_key or explicit_api_key.endswith(".txt"))
        ):
            return os.environ.get("VLLM_API_KEY")
        return explicit_api_key
    return os.environ.get("VLLM_API_KEY")


def resolve_qwen3_base_url() -> str | None:
    for env_var in QWEN3_BASE_URL_ENV_VARS:
        value = os.environ.get(env_var)
        if value:
            return value
    return None


def resolve_qwen3_builtin_endpoint(vision_model: str) -> dict[str, str] | None:
    return QWEN3_BUILTIN_ENDPOINTS.get((vision_model or "").strip().lower())


def resolve_qwen3_model_name(base_url: str | None = None) -> str:
    for env_var in QWEN3_MODEL_ENV_VARS:
        value = os.environ.get(env_var)
        if value:
            return value

    candidate_base_url = base_url or resolve_qwen3_base_url()
    if candidate_base_url:
        parsed = urlparse(candidate_base_url)
        hostname = (parsed.hostname or "").lower()
        if hostname in QWEN3_BASE_URL_MODEL_ALIASES:
            return QWEN3_BASE_URL_MODEL_ALIASES[hostname]

    return DEFAULT_QWEN3_MODEL
