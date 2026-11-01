from __future__ import annotations

from importlib import import_module
from typing import Protocol, TypedDict, cast
from wirecap.types import JSONObject
from .common import QuestionContent
from .provider_types import BackendResult, BackendManyResults, ThreadResult, completed_results, parse_response
from .common import TaskSpec, Question
from .exceptions import GPTOutputParseException, GPTMaxTriesExceededException
import threading
from loguru import logger

class OllamaMessage(TypedDict):
    role: str
    content: list[QuestionContent]


class OllamaPayload(TypedDict):
    messages: OllamaMessage


class OllamaSDK(Protocol):
    def chat(self, *, model: str, messages: list[JSONObject]) -> JSONObject: ...


ollama = cast(OllamaSDK, import_module("ollama"))


class OllamaModel(object):
    def __init__(self,
                 task:TaskSpec,
                 model:str):
        self.task:TaskSpec = task
        self.model:str = model

    def ask(self, payload: OllamaPayload, n_choices: int = 1) -> tuple[list[JSONObject], list[JSONObject]]:
        """
        args:
            payload: json dictionary, prepared by `prepare_payload`
        """

        def ollama_thread(idx: int, payload: OllamaPayload, results: list[ThreadResult | None]) -> None:

            strings: list[str] = []
            for part in payload["messages"]["content"]:
                if part["type"] != "text":
                    raise ValueError("this Ollama adapter only accepts text questions")
                strings.append(part["text"])
            message: JSONObject = {"role": payload["messages"]["role"], "content": "\n".join(strings)}
            try:
                response = ollama.chat(model=self.model, messages=[
                        message])
            except Exception as e:
                raise e
            received_message = response["message"]
            if not isinstance(received_message, dict):
                raise TypeError("Ollama returned an invalid message")
            metadata = response.copy()
            del metadata["message"]
            results[idx] = {"message": received_message, "metadata": metadata}
            return

        assert n_choices >= 1
        results: list[ThreadResult | None] = [None] * n_choices
        if n_choices > 1:
            ollama_jobs = [threading.Thread(target=ollama_thread,
                              args=(idx, payload, results))
                                for idx in range(n_choices)]
            for job in ollama_jobs:
                job.start()
            for job in ollama_jobs:
                job.join()
        else:
            ollama_thread(0, payload, results)
        complete = completed_results(results)
        messages = [res["message"] for res in complete]
        metadata = [res["metadata"] for res in complete]
        return messages, metadata

    @staticmethod
    def prepare_payload(question:Question,
            verbose:bool=False,
            prepend: object = None,
            **kwargs: object
            ) -> OllamaPayload:


        payload: OllamaPayload = {
            "messages": {
                'role': 'user',
                'content': question.get_json()
            },
        }

        return payload


    def rough_guess(self, question:Question,
                    max_tries: int = 1, query_id:int=0,
                    verbose: bool = False,
                    **kwargs: object) -> BackendResult:

        p = self.prepare_payload(question, verbose=verbose, prepend=None,
                                    model=self.model)

        reattempt = 0
        while True:
            responses, meta_data = self.ask(p)
            response = responses[0]
            try:
                parsed_response = parse_response(self.task, response)
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


    def many_rough_guesses(self, num_threads:int,
                           question:Question,
                           verbose: bool = False, max_tries: int = 1,
                           **kwargs: object) -> BackendManyResults:
        """
        Args:
            num_threads : number of independent threads.
            all other  arguments are same as those of `rough_guess()`

        Returns
            List of elements, each element is a tuple following the
            return signature of `rough_guess()`
        """

        p = self.prepare_payload(question, verbose=verbose, prepend=None,
                                    model=self.model)

        #  TODO
        n_choices = num_threads

        # TODO: wrap in robust-ask method, repeatedly asks until parseable output.
        reattempt = 0
        while True:
            response, meta_data = self.ask(p, n_choices=n_choices)
            try:
                parsed_response = [parse_response(self.task, r) for r in response]
            except GPTOutputParseException:
                # logger.warning(f"The following was not parseable:\n\n{response}\n\nBecause\n\n{e}")

                # TODO provide the parse error message into GPT for the next round to be parsable
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p

    def run_once(self, question: Question, *, max_tries: int = 1, query_id: int = 0,
                 verbose: bool = False, **kwargs: object) -> BackendResult:
        q = self.task.first_question(question)
        p_ans, ans, meta, p = self.rough_guess(q, max_tries=max_tries, query_id=query_id, verbose=verbose, **kwargs)
        return p_ans, ans, meta, p
