import os
import threading
from copy import deepcopy
from typing import List, Tuple, Union

import anthropic
from loguru import logger

from .common import ParsedAnswer, Question, TaskSpec, attach_response_metadata
from .exceptions import GPTMaxTriesExceededException, GPTOutputParseException

KIMI_BASE_URL = "https://api.kimi.com/coding"
KIMI_DEFAULT_MODEL = "k2p6"
KIMI_DEFAULT_HEADERS = {"User-Agent": "KimiCLI/1.12.0"}


def resolve_moonshot_api_key(explicit_api_key=None):
    if explicit_api_key:
        return explicit_api_key
    return os.environ.get("MOONSHOT_API_KEY")


class KimiModel(object):
    def __init__(
        self,
        api_key: str,
        task: TaskSpec,
        model: str = KIMI_DEFAULT_MODEL,
        thinking: bool = True,
    ):
        self.moonshot_key: str = api_key
        self.task: TaskSpec = task
        self.model: str = model
        self.thinking: bool = thinking
        self.client = anthropic.Anthropic(
            api_key=self.moonshot_key,
            base_url=KIMI_BASE_URL,
            default_headers=KIMI_DEFAULT_HEADERS,
        )

    @staticmethod
    def _combine_text_blocks(content_blocks) -> Union[str, None]:
        text_blocks = []
        for block in content_blocks or []:
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    text_blocks.append(text)
        if not text_blocks:
            return None
        return "\n".join(text_blocks).strip()

    @staticmethod
    def _combine_thinking_blocks(content_blocks) -> Union[str, None]:
        thinking_blocks = []
        for block in content_blocks or []:
            if block.get("type") == "thinking":
                thinking = block.get("thinking", "")
                if thinking:
                    thinking_blocks.append(thinking)
        if not thinking_blocks:
            return None
        return "\n\n".join(thinking_blocks).strip()

    @staticmethod
    def _summarize_empty_response(response: dict, model: str, max_tokens: int) -> str:
        stop_reason = response.get("stop_reason")
        content_types = [block.get("type") for block in response.get("content", [])]
        return (
            "Kimi returned no visible text content "
            f"for model={model}. Requested max_tokens={max_tokens}. "
            f"stop_reason={stop_reason!r}, content_types={content_types}."
        )

    def ask(self, payload: dict, n_choices=1) -> Tuple[List[dict], List[dict]]:
        def kimi_thread(idx, request_payload, results):
            mod_payload = deepcopy(request_payload)
            request_kwargs = {}
            if self.thinking:
                request_kwargs["thinking"] = {"type": "enabled"}

            raw_response = self.client.messages.create(
                model=self.model,
                messages=[mod_payload["messages"]],
                max_tokens=mod_payload["max_tokens"],
                **request_kwargs,
            )

            response = raw_response.dict()
            text_content = self._combine_text_blocks(response.get("content"))
            thinking_content = self._combine_thinking_blocks(response.get("content"))
            message = {"role": response.get("role", "assistant"), "content": text_content}
            metadata = response.copy()
            metadata["explicit_reasoning_output"] = thinking_content
            results[idx] = {"message": message, "metadata": metadata}

        assert n_choices >= 1
        results = [None] * n_choices
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

        messages: List[dict] = [res["message"] for res in results]
        metadata: List[dict] = [res["metadata"] for res in results]
        return messages, metadata

    @staticmethod
    def prepare_payload(
        question: Question,
        max_tokens=1000,
        verbose: bool = False,
        prepend: Union[dict, None] = None,
        **kwargs,
    ) -> dict:
        content = []
        dic_list = question.get_json()
        for dic in dic_list:
            if dic["type"] == "text":
                content.append(dic)
            elif dic["type"] == "image_url":
                base64enc_image = dic["image_url"]["url"].split(",", 1)[1]
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
                            "media_type": f"image/{image_format}",
                            "data": base64enc_image,
                        },
                    }
                )

        payload = {
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
        max_tokens=1000,
        max_tries=1,
        query_id: int = 0,
        verbose=False,
        **kwargs,
    ):
        p = self.prepare_payload(
            question,
            max_tokens=max_tokens,
            verbose=verbose,
            prepend=None,
            model=self.model,
        )

        ok = False
        reattempt = 0
        while not ok:
            response, meta_data = self.ask(p)
            response = response[0]
            try:
                content = response.get("content")
                if content is None:
                    raise GPTOutputParseException(
                        self._summarize_empty_response(meta_data[0], self.model, p["max_tokens"])
                    )
                parsed_response = attach_response_metadata(
                    self.task.answer_type.parser(content),
                    response_metadata=meta_data[0],
                    request_payload=p,
                )
            except GPTOutputParseException:
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            ok = True

        return parsed_response, response, meta_data, p

    def many_rough_guesses(
        self,
        num_threads: int,
        question: Question,
        max_tokens=1000,
        verbose=False,
        max_tries=1,
    ) -> List[Tuple[ParsedAnswer, str, dict, dict]]:
        p = self.prepare_payload(
            question,
            max_tokens=max_tokens,
            verbose=verbose,
            prepend=None,
            model=self.model,
        )

        ok = False
        reattempt = 0
        while not ok:
            response, meta_data = self.ask(p, n_choices=num_threads)
            try:
                parsed_response = []
                for idx, message in enumerate(response):
                    content = message.get("content")
                    if content is None:
                        raise GPTOutputParseException(
                            self._summarize_empty_response(meta_data[idx], self.model, p["max_tokens"])
                        )
                    parsed = attach_response_metadata(
                        self.task.answer_type.parser(content),
                        response_metadata=meta_data[idx],
                        request_payload=p,
                    )
                    parsed_response.append(parsed)
            except GPTOutputParseException:
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            ok = True

        return parsed_response, response, meta_data, p

    def run_once(self, question: Question, max_tokens=1000, **kwargs):
        q = self.task.first_question(question)
        return self.rough_guess(q, max_tokens=max_tokens, **kwargs)
