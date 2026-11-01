from __future__ import annotations

from wirecap.types import JSONObject
from .provider_types import BackendResult, BackendManyResults, parse_response, response_text
import os
from .common import TaskSpec, Question, attach_response_metadata
from .exceptions import GPTOutputParseException, GPTMaxTriesExceededException
import threading
import base64
import io
from typing import Protocol, TypedDict, cast
from importlib import import_module
from loguru import logger
from google.generativeai.types import generation_types
import time
from PIL import Image

class GeminiPayload(TypedDict):
    messages: list[str | Image.Image]
    max_tokens: int


class GeminiResponse(Protocol):
    @property
    def text(self) -> str: ...


class GeminiClient(Protocol):
    def generate_content(self, *, contents: list[str | Image.Image],
                         generation_config: generation_types.GenerationConfig) -> GeminiResponse: ...


class GeminiSDK(Protocol):
    def configure(self, *, api_key: str | None) -> None: ...
    def GenerativeModel(self, *, model_name: str, safety_settings: None,
                        generation_config: None) -> GeminiClient: ...


class GeminiResult(TypedDict):
    message: JSONObject
    metadata: GeminiResponse


# The legacy SDK exposes untyped protobuf unions; describe the API used here.
genai = cast(GeminiSDK, import_module("google.generativeai"))


class GeminiModel(object):
    def __init__(self, api_key: str | None,
                 task:TaskSpec,
                 model:str="gemini-2.0-flash"):
        self.gemini_key: str | None = api_key
        self.task:TaskSpec = task
        self.model:str = model


    def ask(self, payload: GeminiPayload, n_choices: int = 1) -> tuple[list[JSONObject], list[GeminiResponse]]:
        """
        args:
            payload: json dictionary, prepared by `prepare_payload`
        """

        def gemini_thread(idx: int, payload: GeminiPayload, results: list[GeminiResult | None]) -> None:


            config_instance = generation_types.GenerationConfig(
                max_output_tokens=payload["max_tokens"],
            )

            try:
                raw_response = client.generate_content(
                    contents=payload["messages"],
                    generation_config=config_instance
                )
            except Exception as e:
                raise e

            response: JSONObject = {'content': raw_response.text}
            results[idx] = {"message": response, "metadata": raw_response}
            return

        genai.configure(api_key=self.gemini_key)
        client = genai.GenerativeModel(model_name=self.model,
            safety_settings= None,
            generation_config = None
        )

        assert n_choices >= 1
        results: list[GeminiResult | None] = [None] * n_choices
        if n_choices > 1:
            gemini_jobs = [threading.Thread(target=gemini_thread,
                              args=(idx, payload, results))
                                for idx in range(n_choices)]
            for job in gemini_jobs:
                job.start()
            for job in gemini_jobs:
                job.join()
        else:
            gemini_thread(0, payload, results)
        if any(result is None for result in results):
            raise RuntimeError("a Gemini request failed before producing a response")
        complete = [result for result in results if result is not None]
        messages = [result["message"] for result in complete]
        metadata = [result["metadata"] for result in complete]
        return messages, metadata


    @staticmethod
    def prepare_payload(question:Question,
            max_tokens: int = 1000,
            verbose:bool=False,
            prepend: object = None,
            **kwargs: object
            ) -> GeminiPayload:

        strings: list[str] = []
        images: list[Image.Image] = []
        # inline images only: the data URL is decoded below, so no copy is written to disk
        for el in question.get_json():
            if el['type'] == 'text':
                strings.append(el['text'])
            else:
                #Convert the binary encoded version to PIL.image
                base64enc_image = el['image_url']['url'].split(',', 1)[1]
                base64_image_str = base64enc_image  # the Base64-encoded string
                image_data = base64.b64decode(base64_image_str)
                image_data_io = io.BytesIO(image_data)

                # Read the image from the BytesIO object
                pil_image = Image.open(image_data_io)

                images.append(pil_image)

        string_message = "\n".join(strings)
        messages: list[str | Image.Image] = [string_message]

        for image in images:
            messages.append(image)

        payload: GeminiPayload = {
            "messages": messages,
            "max_tokens": max_tokens,
        }

        return payload


    def rough_guess(self, question:Question, max_tokens: int = 1000,
                    max_tries: int = 1, query_id:int=0,
                    verbose: bool = False,
                    **kwargs: object) -> BackendResult:

        p = self.prepare_payload(question, max_tokens = max_tokens, verbose=verbose, prepend=None,
                                    model=self.model)

        reattempt = 0
        while True:
            responses, meta_data = self.ask(p)
            response = responses[0]
            try:
                parsed_response = attach_response_metadata(
                    parse_response(self.task, response),
                    response_metadata=meta_data[0] if len(meta_data) > 0 else meta_data,
                    request_payload=p,
                )
            except GPTOutputParseException:

                if not os.path.exists('errors/'):
                    # Create the directory if it doesn't exist
                    os.makedirs('errors/')

                error_saved = f'errors/{time.strftime("%Y-%m-%d-%H-%M-%S")}.json'
                with open(error_saved, "w")  as f:
                    f.write(response_text(response))
                # logger.warning(f"The following was not parseable. Saved in {error_saved}.")

                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p


    def many_rough_guesses(self, num_threads:int,
                           question:Question, max_tokens: int = 1000,
                           verbose: bool = False, max_tries: int = 1,
                           ) -> BackendManyResults:
        """
        Args:
            num_threads : number of independent threads.
            all other  arguments are same as those of `rough_guess()`

        Returns
            List of elements, each element is a tuple following the
            return signature of `rough_guess()`
        """

        p = self.prepare_payload(question, max_tokens = max_tokens, verbose=verbose, prepend=None,
                                    model=self.model)
        #print('In many rough: ', p)

        n_choices = num_threads

        reattempt = 0
        while True:
            response, meta_data = self.ask(p, n_choices=n_choices)
            try:
                parsed_response = [
                    attach_response_metadata(
                        parse_response(self.task, r),
                        response_metadata=meta_data[idx] if len(meta_data) > idx else None,
                        request_payload=p,
                    )
                    for idx, r in enumerate(response)
                ]
            except GPTOutputParseException:
                # logger.warning(f"The following was not parseable:\n\n{response}\n\nBecause\n\n{e}")

                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p

    def run_once(self, question:Question, max_tokens: int = 1000, *, max_tries: int = 1, query_id: int = 0, verbose: bool = False, **kwargs: object) -> BackendResult:
        q = self.task.first_question(question)
        p_ans, ans, meta, p = self.rough_guess(q, max_tokens=max_tokens, max_tries=max_tries, query_id=query_id, verbose=verbose)
        return p_ans, ans, meta, p
