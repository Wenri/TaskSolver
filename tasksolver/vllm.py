import os
from urllib.parse import urlparse
from typing import List, Tuple, Union

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


class VLLMModel(object):
    def __init__(
        self,
        api_key: str,
        task: TaskSpec,
        model: str = DEFAULT_QWEN3_MODEL,
        base_url: str = None,
    ):
        self.api_key = api_key
        self.task = task
        self.model = model
        self.base_url = base_url

    def _normalize_max_tokens(self, max_tokens: int) -> int:
        return max(max_tokens, MIN_QWEN3_MAX_TOKENS)

    @staticmethod
    def _default_extra_body() -> dict:
        return {
            "chat_template_kwargs": {
                "enable_thinking": False,
            },
        }

    @staticmethod
    def _extract_message_text(message: dict):
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )

        if content:
            return content

        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, list):
            reasoning = "".join(
                part.get("text", "")
                for part in reasoning
                if isinstance(part, dict)
            )

        if isinstance(reasoning, str) and "```python" in reasoning:
            return reasoning

        return content

    @staticmethod
    def _summarize_empty_response(message: dict, model: str, max_tokens: int) -> str:
        finish_reason = message.get("finish_reason")
        has_reasoning = bool(message.get("reasoning_content"))
        return (
            "The vLLM endpoint returned an empty visible response "
            f"for model={model}. Requested max_tokens={max_tokens}. "
            f"finish_reason={finish_reason!r}, reasoning_content_present={has_reasoning}."
        )

    def ask(self, payload: dict, n_choices=1) -> Tuple[dict, dict]:
        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=180.0,
        )
        response = client.chat.completions.create(
            model=self.model,
            messages=payload["messages"],
            max_tokens=payload["max_tokens"],
            n=n_choices,
            extra_body=self._default_extra_body(),
        )

        response = response.dict()
        messages = []
        for choice in response["choices"]:
            message = choice["message"]
            message["finish_reason"] = choice.get("finish_reason")
            message["explicit_reasoning_output"] = extract_explicit_reasoning_output(message)
            message["content"] = self._extract_message_text(message)
            messages.append(message)

        return messages, response.get("usage")

    @staticmethod
    def prepare_payload(
        question: Question,
        verbose: bool = False,
        prepend: Union[dict, None] = None,
        model: str = DEFAULT_QWEN3_MODEL,
        max_tokens: int = 4096,
    ) -> dict:
        question_dicts = question.get_json()

        for part in question_dicts:
            if part["type"] == "image_url":
                del part["image"]

        payload = [{"role": "user", "content": question_dicts}]
        if prepend is not None:
            payload = [prepend] + payload

        return {
            "model": model,
            "messages": payload,
            "max_tokens": max_tokens,
        }

    def many_rough_guesses(
        self,
        num_threads: int,
        question: Question,
        max_tokens=4096,
        verbose=False,
        max_tries=1,
    ) -> List[Tuple[ParsedAnswer, str, dict, dict]]:
        p = self.prepare_payload(
            question,
            verbose=verbose,
            prepend=None,
            model=self.model,
            max_tokens=self._normalize_max_tokens(max_tokens),
        )

        ok = False
        reattempt = 0
        while not ok:
            response, meta_data = self.ask(p, n_choices=num_threads)
            try:
                parsed_response = []
                for message in response:
                    content = message.get("content")
                    if content is None:
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
            ok = True

        return parsed_response, response, meta_data, p

    def rough_guess(
        self,
        question: Question,
        max_tokens=4096,
        verbose=False,
        max_tries=1,
        query_id: int = 0,
    ) -> Tuple[ParsedAnswer, str, dict, dict]:
        p = self.prepare_payload(
            question,
            verbose=verbose,
            prepend=None,
            model=self.model,
            max_tokens=self._normalize_max_tokens(max_tokens),
        )

        ok = False
        reattempt = 0
        while not ok:
            response, meta_data = self.ask(p)
            response = response[0]
            try:
                content = response.get("content")
                if content is None:
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
            ok = True
        return parsed_response, response, meta_data, p

    def run_once(self, question: Question, max_tokens=4096, **kwargs):
        q = self.task.first_question(question)
        return self.rough_guess(q, max_tokens=max_tokens, **kwargs)


def resolve_qwen3_api_key(explicit_api_key=None):
    if explicit_api_key:
        if (
            isinstance(explicit_api_key, str)
            and not os.path.exists(explicit_api_key)
            and ("/" in explicit_api_key or explicit_api_key.endswith(".txt"))
        ):
            return os.environ.get("VLLM_API_KEY")
        return explicit_api_key
    return os.environ.get("VLLM_API_KEY")


def resolve_qwen3_base_url():
    for env_var in QWEN3_BASE_URL_ENV_VARS:
        value = os.environ.get(env_var)
        if value:
            return value
    return None


def resolve_qwen3_builtin_endpoint(vision_model: str):
    return QWEN3_BUILTIN_ENDPOINTS.get((vision_model or "").strip().lower())


def resolve_qwen3_model_name(base_url=None):
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
