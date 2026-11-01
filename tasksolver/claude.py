from __future__ import annotations

from wirecap.types import JSONObject
from .provider_types import BackendResult, BackendManyResults, parse_response, response_text
from anthropic.types import ContentBlockParam
from .provider_types import AnthropicPayload, ThreadResult, completed_results, media_type, object_list
import anthropic
from .common import TaskSpec, Question, attach_response_metadata, extract_explicit_reasoning_output
from .exceptions import GPTOutputParseException, GPTMaxTriesExceededException
import threading
from typing import Literal, cast
from loguru import logger
from copy import deepcopy
import time
import os

CLAUDE_MODEL_ALIASES = {
    "claude-3.7-sonnet-latest": "claude-3-7-sonnet-latest",
}


class ClaudeModel(object):
    def __init__(self, api_key: str | None,
                 task:TaskSpec,
                 model:str = "claude-sonnet-4-6"):

        self.claude_key: str | None = api_key
        self.task:TaskSpec = task
        self.model:str = CLAUDE_MODEL_ALIASES.get(model, model)

    def ask(self, payload: AnthropicPayload, n_choices: int = 1) -> tuple[list[JSONObject], list[JSONObject]]:
        """
        args:
            payload: json dictionary, prepared by `prepare_payload`
        """

        def claude_thread(idx: int, payload: AnthropicPayload, results: list[ThreadResult | None]) -> None:

            # creation of payload
            mod_payload = deepcopy(payload)

            try:
                raw_response = client.messages.create(
                    model=self.model,
                    #messages=[{"role": "user", "content": "Hello, Claude, tell me a number between 1 to 10000 please."}],
                    messages = [mod_payload["messages"]],
                    max_tokens=mod_payload["max_tokens"],
                )
            except Exception as e:
                raise e

            response = cast(JSONObject, raw_response.model_dump())
            text_blocks: list[str] = []
            for block in object_list(response.get("content")):
                text = block.get("text")
                if block.get("type") == "text" and isinstance(text, str) and text:
                    text_blocks.append(text.strip())
            response['content'] = "\n".join(text_blocks).strip() if text_blocks else None
            response["explicit_reasoning_output"] = extract_explicit_reasoning_output(response)
            message: JSONObject = {key: response.get(key) for key in ['role', 'content']}
            metadata = response.copy() # okay
            del metadata["content"]
            results[idx] = {"message": message, "metadata": metadata}
            return

        client = anthropic.Anthropic(api_key = self.claude_key)

        assert n_choices >= 1
        results: list[ThreadResult | None] = [None] * n_choices
        if n_choices > 1:
            claude_jobs = [threading.Thread(target=claude_thread,
                              args=(idx, payload, results))
                                for idx in range(n_choices)]
            for job in claude_jobs:
                job.start()
            for job in claude_jobs:
                job.join()
        else:
            claude_thread(0, payload, results)
        complete = completed_results(results)
        messages = [res["message"] for res in complete]
        metadata = [res["metadata"] for res in complete]
        return messages, metadata


    @staticmethod
    def prepare_payload(question:Question,
            max_tokens: int = 1000,
            verbose:bool=False,
            prepend: object = None,
            **kwargs: object
            ) -> AnthropicPayload:

        content: list[ContentBlockParam] = []
        dic_list = question.get_json()
        for dic in dic_list:
            # The case of text
            if dic['type'] == 'text':
                content.append({"type": "text", "text": dic["text"]})

            # The case of vision input
            elif dic['type'] == 'image_url':
                base64enc_image = dic['image_url']['url'].split(',')[1]
                image_format: Literal["jpeg", "png", "gif", "webp"]
                if base64enc_image.startswith("/9j/"):
                    image_format = 'jpeg'
                elif base64enc_image.startswith("iVBORw0KGgo"):
                    image_format = "png"
                elif base64enc_image.startswith("R0lGOD") or base64enc_image.startswith("R0lGOD"):
                    image_format =  "gif"
                elif base64enc_image.startswith("UklGR"):
                    image_format =  "webp"
                else:
                    raise ValueError("Unknown format")

                modified_dic: ContentBlockParam = {
                    'type' : "image",
                    'source' : {
                        'type' : "base64",
                        'media_type': media_type(image_format),
                        'data' : base64enc_image
                    }
                }

                content.append(modified_dic)

        payload: AnthropicPayload = {
            "messages": {
                'role': 'user',
                'content': content
            },
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
