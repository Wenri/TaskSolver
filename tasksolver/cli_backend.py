"""Shared question preparation and parse retries for agent-runtime backends.

Subclasses supply ``_client_ask_many`` and ``_call_kwargs``, implement ``_sample``
with their client's typed options, or override ``ask``.
This module belongs to parent-side model adapters; embedded CLI modules must not import it.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Literal, NotRequired, Protocol, TypedDict, cast

from wirecap.types import JSONObject

from loguru import logger

from .common import ParsedAnswer, Question, ResponseMetadata, TaskSpec, TextContent, attach_response_metadata
from .exceptions import GPTMaxTriesExceededException, GPTOutputParseException

class InlineImage(TypedDict):
    type: Literal["image"]
    url: str


type InlineContent = TextContent | InlineImage


class CLIPayload(TypedDict):
    prompt: str | list[JSONObject]
    max_tokens: NotRequired[int]
    workspace: NotRequired[str | None]
    image_paths: NotRequired[list[str]]
    content: NotRequired[list[InlineContent]]


class Message(TypedDict):
    role: Literal["assistant"]
    content: str


class CLIResponse(Protocol):
    @property
    def text(self) -> str: ...
    @property
    def transcript(self) -> str: ...
    @property
    def exit_status(self) -> int | None: ...
    @property
    def workspace(self) -> str | None: ...
    @property
    def model(self) -> str | None: ...
    @property
    def usage(self) -> object: ...


class _LegacySampler[ResponseT: CLIResponse](Protocol):
    def __call__(self, prompt: str | list[JSONObject], n: int,
                 **kwargs: object) -> Sequence[ResponseT]: ...


type CLIResult = tuple[ParsedAnswer, Message, list[ResponseMetadata], CLIPayload]
type CLIManyResults = tuple[list[ParsedAnswer], list[Message], list[ResponseMetadata], CLIPayload]


def string_prompt(payload: CLIPayload) -> str:
    prompt = payload["prompt"]
    if not isinstance(prompt, str):
        raise TypeError("this backend requires a text prompt")
    return prompt


#: workspace subdirectory for the image files handed to file-reading agents
WORKSPACE_IMAGE_DIR = ".tasksolver-images"


class ChatModeViolation(RuntimeError):
    """A chat-mode turn used a tool (or otherwise acted as an agent); ``response`` keeps it."""

    def __init__(self, message: str, response: object = None) -> None:
        super().__init__(message)
        self.response = response


class CLIBackendModel[ResponseT: CLIResponse = CLIResponse]:
    # --- per-backend knobs (class attributes) ---------------------------------
    backend_label: str = "LLM"          #: the retry log's "querying <label>"
    command_label: str | None = None           #: empty-output error subject (default: backend_label)
    generic_model_aliases: tuple[str, ...] = ()   #: model ids normalized to None = "let the CLI pick"
    no_output_hint: str = ""            #: how to fix an empty reply (auth / install)
    vision_preamble: str = ("The visual inputs are saved as local image files. "
                            "Use the Read tool to inspect them when answering.")
    #: adapters that shell into a git workspace set this per instance; the rest keep the None default
    workspace: str | None = None
    #: enabled only by backends that implement chat mode
    chat: bool = False

    def __init__(self, api_key: str | None = None, task: TaskSpec | None = None,
                 model: str | None = None) -> None:
        self.api_key = api_key
        self.task: TaskSpec | None = task
        self.model: str | None = model if model not in (None, *self.generic_model_aliases) else None

    # --- payload --------------------------------------------------------------
    @classmethod
    def prepare_payload(cls, question: Question, max_tokens: int = 1000, verbose: bool = False,
                        prepend: object = None, workspace: str | None = None, inline: bool = False,
                        **kwargs: object) -> CLIPayload:
        """Flatten a Question into ``{prompt, max_tokens, workspace, image_paths}``. Image
        elements are saved as PNG files (inside ``workspace`` when given, so a sandboxed agent
        can open them) and announced by absolute path after ``cls.vision_preamble``.

        ``inline=True`` instead keeps the Question's order as inline ``content`` parts —
        ``{"type": "text", "text"}`` / ``{"type": "image", "url": <data URL>}`` — and writes no
        files; ``prompt`` is then just the joined text, for logs.

        The classmethod keeps the preamble and error messages specific to the subclass.
        """
        if inline:
            content: list[InlineContent] = []
            for dic in question.get_json():
                if dic["type"] == "text":
                    content.append({"type": "text", "text": dic["text"]})
                elif dic["type"] == "image_url":
                    content.append({"type": "image", "url": dic["image_url"]["url"]})
            return {"prompt": "\n\n".join(p["text"] for p in content if p["type"] == "text"),
                    "content": content, "max_tokens": max_tokens, "workspace": workspace,
                    "image_paths": []}

        save_dir = os.path.join(workspace, WORKSPACE_IMAGE_DIR) if workspace else None
        strings: list[str] = []
        image_paths: list[str] = []
        for dic in question.get_json(save_local=True, save_dir=save_dir):
            if dic["type"] == "text":
                strings.append(dic["text"])
            elif dic["type"] == "image_url":
                local_path = dic.get("local_path")
                if local_path is None:
                    image = dic.get("image")
                    if image is None:
                        raise ValueError(f"{cls.__name__} needs local image files for vision inputs.")
                    local_path = Question.get_pil_image_content_savecopy(image, save_dir).get("local_path")
                    assert local_path is not None
                image_paths.append(os.path.abspath(local_path))

        parts: list[str] = []
        if image_paths:
            parts.append(cls.vision_preamble)
            parts.extend(f"Image {i}: {p}" for i, p in enumerate(image_paths, 1))
        parts.extend(strings)
        return {"prompt": "\n\n".join(parts), "max_tokens": max_tokens,
                "workspace": workspace, "image_paths": image_paths}

    # --- the provider call ----------------------------------------------------
    def _call_kwargs(self, payload: CLIPayload) -> Mapping[str, object]:
        """Options forwarded to a subclass's ``_client_ask_many`` callable."""
        return {}

    def _sample(self, payload: CLIPayload, n_choices: int) -> Sequence[ResponseT]:
        # Subclasses supply the callable and own its keyword contract.
        sampler = cast(_LegacySampler[ResponseT] | None,
                       getattr(self, "_client_ask_many", None))
        if sampler is None:
            raise NotImplementedError(
                f"{type(self).__name__} must set _client_ask_many or override _sample()/ask()")
        return sampler(payload["prompt"], n_choices, **self._call_kwargs(payload))

    def _check_output(self, r: ResponseT) -> None:
        """Raise when the CLI produced no answer at all (auth / install failures land here)."""
        if not r.text:
            raise RuntimeError(
                f"{self.command_label or self.backend_label} returned no output "
                f"(exit_status={r.exit_status}, workspace={r.workspace}). "
                f"{self.no_output_hint}\nTranscript head:\n{r.transcript[:500]}")

    def _finish(self, r: ResponseT) -> ResponseMetadata:
        """Validate one client response object and shape the per-choice result dict."""
        self._check_output(r)
        if self.chat:
            self._check_chat(r)
        return {"result": r.text, "transcript": r.transcript, "exit_status": r.exit_status,
                "workspace": r.workspace, "model": r.model, "usage": r.usage}

    def _inline_images(self) -> bool:
        """Does this model send images inline (``prepare_payload(inline=True)``)? Chat mode does,
        on the backends whose runtime accepts inline images."""
        return self.chat

    def _chat_violations(self, r: ResponseT) -> list[str]:
        """Names of the tools (or other agent actions) a chat-mode response ``r`` used.
        Backends that support chat mode override this; the base finds none."""
        return []

    def _check_chat(self, r: ResponseT) -> None:
        """Raise :class:`ChatModeViolation` when a chat-mode turn acted as an agent."""
        used = self._chat_violations(r)
        if used:
            raise ChatModeViolation(
                f"{self.command_label or self.backend_label} used {', '.join(map(str, used))} "
                "in chat mode", response=r)

    def ask(self, payload: CLIPayload, n_choices: int = 1) -> tuple[list[Message], list[ResponseMetadata]]:
        """Return answer messages and per-choice metadata from the provider client."""
        assert n_choices >= 1
        results = [self._finish(response) for response in self._sample(payload, n_choices)]
        messages: list[Message] = []
        for result in results:
            content = result["result"]
            if not isinstance(content, str):
                raise TypeError("a client result must contain text")
            messages.append({"role": "assistant", "content": content})
        return messages, results

    # --- retry-on-parse-failure (the universal loop) --------------------------
    def _max_tries_exceeded(self, max_tries: int, reattempt: int, exc: Exception,
                            raw_response: object, response_metadata: object,
                            request_payload: object) -> GPTMaxTriesExceededException:
        """Build the parse-failure exception with the failed attempt's context."""
        logger.error(f"max tries ({max_tries}) exceeded.")
        return GPTMaxTriesExceededException(
            f"Failed to parse {self.backend_label} response after {reattempt} attempts: {exc}",
            raw_response=raw_response,
            response_metadata=response_metadata,
            request_payload=request_payload,
        )

    def rough_guess(self, question: Question, max_tokens: int = 1000, max_tries: int = 1,
                    query_id: int = 0, verbose: bool = False, **kwargs: object) -> CLIResult:
        if self.task is None:
            raise ValueError("a task is required to parse a response")
        p = self.prepare_payload(question, max_tokens=max_tokens, verbose=verbose,
                                 prepend=None, workspace=self.workspace,
                                 inline=self._inline_images())
        reattempt = 0
        while True:
            responses, meta_data = self.ask(p)
            response = responses[0]
            try:
                parsed_response = attach_response_metadata(
                    self.task.answer_type.parser(response["content"]),
                    response_metadata=(meta_data[0] if meta_data
                                       else meta_data),
                    request_payload=p,
                )
            except GPTOutputParseException as exc:
                reattempt += 1
                if reattempt > max_tries:
                    raise self._max_tries_exceeded(
                        max_tries, reattempt, exc, response, meta_data, p) from exc
                logger.warning(f"Reattempt #{reattempt} querying {self.backend_label}")
                continue
            return parsed_response, response, meta_data, p

    def many_rough_guesses(self, num_threads: int, question: Question, max_tokens: int = 1000,
                           verbose: bool = False, max_tries: int = 1) -> CLIManyResults:
        if self.task is None:
            raise ValueError("a task is required to parse responses")
        p = self.prepare_payload(question, max_tokens=max_tokens, verbose=verbose,
                                 prepend=None, workspace=self.workspace,
                                 inline=self._inline_images())
        reattempt = 0
        while True:
            response, meta_data = self.ask(p, n_choices=num_threads)
            try:
                parsed_response = [
                    attach_response_metadata(
                        self.task.answer_type.parser(r["content"]),
                        response_metadata=(meta_data[idx]
                                           if len(meta_data) > idx
                                           else None),
                        request_payload=p,
                    )
                    for idx, r in enumerate(response)
                ]
            except GPTOutputParseException as exc:
                reattempt += 1
                if reattempt > max_tries:
                    raise self._max_tries_exceeded(
                        max_tries, reattempt, exc, response, meta_data, p) from exc
                logger.warning(f"Reattempt #{reattempt} querying {self.backend_label}")
                continue
            return parsed_response, response, meta_data, p

    def run_once(self, question: Question, max_tokens: int = 1000, *, max_tries: int = 1,
                 query_id: int = 0, verbose: bool = False, **kwargs: object) -> CLIResult:
        if self.task is None:
            raise ValueError("a task is required to build a question")
        q = self.task.first_question(question)
        return self.rough_guess(q, max_tokens=max_tokens, max_tries=max_tries, query_id=query_id,
                                verbose=verbose, **kwargs)
