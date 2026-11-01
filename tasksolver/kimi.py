from __future__ import annotations

from wirecap.types import JSONObject, JSONValue
from .provider_types import BackendResult, BackendManyResults
from anthropic.types import ContentBlockParam
from .provider_types import AnthropicPayload, ThreadResult, completed_results, media_type, object_list
import os
import threading
from copy import deepcopy
from typing import Literal, cast

import anthropic
from loguru import logger

from .common import ParsedAnswer, Question, TaskSpec, attach_response_metadata
from .exceptions import GPTMaxTriesExceededException, GPTOutputParseException

KIMI_BASE_URL = "https://api.kimi.com/coding"
KIMI_DEFAULT_MODEL = "k2p6"
KIMI_DEFAULT_HEADERS = {"User-Agent": "KimiCLI/1.12.0"}

# Maps the vision_model id passed via --generator_type / --verifier_type to the
# concrete Moonshot model name expected by the Kimi coding endpoint.
KIMI_MODEL_ALIASES = {
    "kimi2-6": "k2p6",
    "kimi-k2.7-code": "k2p7",
    "kimi2-7": "k2p7",
    "k2p7": "k2p7",
}


def resolve_moonshot_api_key(explicit_api_key: str | None = None) -> str | None:
    if explicit_api_key:
        return explicit_api_key
    return os.environ.get("MOONSHOT_API_KEY")


def resolve_moonshot_model_name(vision_model: str | None = None) -> str:
    if not vision_model:
        return KIMI_DEFAULT_MODEL
    return KIMI_MODEL_ALIASES.get(vision_model.strip(), KIMI_DEFAULT_MODEL)


class KimiModel(object):
    def __init__(
        self,
        api_key: str | None,
        task: TaskSpec,
        model: str = KIMI_DEFAULT_MODEL,
        thinking: bool = False,
    ):
        self.moonshot_key: str | None = api_key
        self.task: TaskSpec = task
        self.model: str = model
        self.thinking: bool = thinking
        self.client = anthropic.Anthropic(
            api_key=self.moonshot_key,
            base_url=KIMI_BASE_URL,
            default_headers=KIMI_DEFAULT_HEADERS,
        )

    @staticmethod
    def _combine_text_blocks(content_blocks: JSONValue) -> str | None:
        text_blocks: list[str] = []
        for block in object_list(content_blocks):
            if block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    text_blocks.append(text)
        if not text_blocks:
            return None
        return "\n".join(text_blocks).strip()

    @staticmethod
    def _combine_thinking_blocks(content_blocks: JSONValue) -> str | None:
        thinking_blocks: list[str] = []
        for block in object_list(content_blocks):
            if block.get("type") == "thinking":
                thinking = block.get("thinking", "")
                if isinstance(thinking, str) and thinking:
                    thinking_blocks.append(thinking)
        if not thinking_blocks:
            return None
        return "\n\n".join(thinking_blocks).strip()

    @staticmethod
    def _summarize_empty_response(response: JSONObject, model: str, max_tokens: int) -> str:
        stop_reason = response.get("stop_reason")
        content_types = [block.get("type") for block in object_list(response.get("content"))]
        return (
            "Kimi returned no visible text content "
            f"for model={model}. Requested max_tokens={max_tokens}. "
            f"stop_reason={stop_reason!r}, content_types={content_types}."
        )

    def ask(self, payload: AnthropicPayload, n_choices: int = 1) -> tuple[list[JSONObject], list[JSONObject]]:
        def kimi_thread(idx: int, request_payload: AnthropicPayload, results: list[ThreadResult | None]) -> None:
            mod_payload = deepcopy(request_payload)

            with self.client.messages.stream(
                model=self.model,
                messages=[mod_payload["messages"]],
                max_tokens=mod_payload["max_tokens"],
            ) as stream:
                stream.until_done()
                raw_response = stream.get_final_message()

            response = cast(JSONObject, raw_response.model_dump())
            text_content = self._combine_text_blocks(response.get("content"))
            thinking_content = self._combine_thinking_blocks(response.get("content"))
            message: JSONObject = {"role": response.get("role", "assistant"), "content": text_content}
            metadata = response.copy()
            metadata["explicit_reasoning_output"] = thinking_content
            results[idx] = {"message": message, "metadata": metadata}

        assert n_choices >= 1
        results: list[ThreadResult | None] = [None] * n_choices
        if n_choices > 1:
            kimi_jobs = [
                threading.Thread(target=kimi_thread, args=(idx, payload, results))
                for idx in range(n_choices)
            ]
            for job in kimi_jobs:
                job.start()
            for job in kimi_jobs:
                job.join()
        else:
            kimi_thread(0, payload, results)

        complete = completed_results(results)
        messages = [res["message"] for res in complete]
        metadata = [res["metadata"] for res in complete]
        return messages, metadata

    @staticmethod
    def prepare_payload(
        question: Question,
        max_tokens: int = 100000,
        verbose: bool = False,
        prepend: object = None,
        **kwargs: object,
    ) -> AnthropicPayload:
        content: list[ContentBlockParam] = []
        dic_list = question.get_json()
        for dic in dic_list:
            if dic["type"] == "text":
                content.append({"type": "text", "text": dic["text"]})
            elif dic["type"] == "image_url":
                base64enc_image = dic["image_url"]["url"].split(",", 1)[1]
                image_format: Literal["jpeg", "png", "gif", "webp"]
                if base64enc_image.startswith("/9j/"):
                    image_format = "jpeg"
                elif base64enc_image.startswith("iVBORw0KGgo"):
                    image_format = "png"
                elif base64enc_image.startswith("R0lGOD"):
                    image_format = "gif"
                elif base64enc_image.startswith("UklGR"):
                    image_format = "webp"
                else:
                    raise ValueError("Unknown format")

                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type(image_format),
                            "data": base64enc_image,
                        },
                    }
                )

        payload: AnthropicPayload = {
            "messages": {
                "role": "user",
                "content": content,
            },
            "max_tokens": max_tokens,
        }
        return payload

    def rough_guess(
        self,
        question: Question,
        max_tokens: int = 100000,
        max_tries: int = 1,
        query_id: int = 0,
        verbose: bool = False,
        **kwargs: object,
    ) -> BackendResult:
        p = self.prepare_payload(
            question,
            max_tokens=max_tokens,
            verbose=verbose,
            prepend=None,
            model=self.model,
        )

        reattempt = 0
        while True:
            responses, meta_data = self.ask(p)
            response = responses[0]
            content: JSONValue = None
            try:
                content = response.get("content")
                if not isinstance(content, str):
                    raise GPTOutputParseException(
                        self._summarize_empty_response(meta_data[0], self.model, p["max_tokens"])
                    )
                parsed_response = attach_response_metadata(
                    self.task.answer_type.parser(content),
                    response_metadata=meta_data[0],
                    request_payload=p,
                )
            except GPTOutputParseException as exc:
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException(
                        f"Failed to parse Kimi response after {reattempt} attempts: {exc}",
                        raw_response=content,
                        response_metadata=meta_data[0],
                        request_payload=p,
                    ) from exc

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p

    def many_rough_guesses(
        self,
        num_threads: int,
        question: Question,
        max_tokens: int = 100000,
        verbose: bool = False,
        max_tries: int = 1,
    ) -> BackendManyResults:
        p = self.prepare_payload(
            question,
            max_tokens=max_tokens,
            verbose=verbose,
            prepend=None,
            model=self.model,
        )

        reattempt = 0
        while True:
            response, meta_data = self.ask(p, n_choices=num_threads)
            content: JSONValue = None
            idx = 0
            try:
                parsed_response: list[ParsedAnswer] = []
                for idx, message in enumerate(response):
                    content = message.get("content")
                    if not isinstance(content, str):
                        raise GPTOutputParseException(
                            self._summarize_empty_response(meta_data[idx], self.model, p["max_tokens"])
                        )
                    parsed = attach_response_metadata(
                        self.task.answer_type.parser(content),
                        response_metadata=meta_data[idx],
                        request_payload=p,
                    )
                    parsed_response.append(parsed)
            except GPTOutputParseException as exc:
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException(
                        f"Failed to parse Kimi response after {reattempt} attempts: {exc}",
                        raw_response=content,
                        response_metadata=meta_data[idx],
                        request_payload=p,
                    ) from exc

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p

    def run_once(self, question: Question, max_tokens: int = 100000, *, max_tries: int = 1, query_id: int = 0, verbose: bool = False, **kwargs: object) -> BackendResult:
        q = self.task.first_question(question)
        return self.rough_guess(q, max_tokens=max_tokens, max_tries=max_tries, query_id=query_id, verbose=verbose)
