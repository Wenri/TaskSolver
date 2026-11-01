from __future__ import annotations

from wirecap.types import JSONObject, JSONValue
from .provider_types import BackendResult, BackendManyResults, parse_response
from openai.types.chat import ChatCompletionMessageParam
from .provider_types import OpenAIPayload, openai_messages
from typing import cast
from loguru import logger
from openai import OpenAI
from .common import AnswerCompletion, CompletionAnswer, TaskSpec, ParsedAnswer, Question, attach_response_metadata
from .exceptions import GPTOutputParseException, GPTMaxTriesExceededException


class GPTModel(object):
    def __init__(self, api_key: str | None,
                 task:TaskSpec,
                 model:str="gpt-4o",
                 ):
        self.open_ai_key: str | None = api_key

        self.task:TaskSpec = task
        self.model:str = model

    def ask(self, payload: OpenAIPayload, n_choices: int = 1) -> tuple[list[JSONObject], JSONValue]:
        client = OpenAI(api_key=self.open_ai_key)
        if self.model in ('o1-preview', 'o1-mini', 'o1'):
            response = client.chat.completions.create(
                model=self.model, messages=payload["messages"],
                max_completion_tokens=payload["max_tokens"], n=n_choices,
            )
        else:
            response = client.chat.completions.create(
                model=self.model, messages=payload["messages"],
                max_tokens=payload["max_tokens"], n=n_choices,
            )
        messages = [cast(JSONObject, choice.message.model_dump()) for choice in response.choices]
        metadata = cast(JSONObject, response.usage.model_dump()) if response.usage is not None else None
        return messages, metadata

    @staticmethod
    def prepare_payload(question: Question, verbose: bool = False,
                        prepend: ChatCompletionMessageParam | None = None,
                        model: str = "gpt-4-vision-preview", max_tokens: int = 1000) -> OpenAIPayload:
        return {"model": model, "messages": openai_messages(question, prepend), "max_tokens": max_tokens}

    def run_once(self, question:Question, max_tokens: int = 1000) -> BackendResult:
        print('Into gpt4v.py -> think')
        q = self.task.first_question(question)
        p_ans, ans, meta, p = self.rough_guess(q, max_tokens=max_tokens)
        return p_ans, ans, meta, p


    ############### NOTE : deprecated -- moved to Agent class.
    def run(self, question: Question, verbose: bool = False) -> BackendResult:
        """ Main running program
        """
        logger.warning("DEPRECATED! Use the Agents class instead!")

        answers_history: list[ParsedAnswer] = []
        questions_history: list[Question] = []
        eval_history: list[bool | CompletionAnswer] = []

        first_q = self.task.first_question(question)
        p_ans, ans, meta, p = self.rough_guess(first_q)

        questions_history.append(question)
        answers_history.append(p_ans)
        latest_answer = p_ans

        if verbose:
            # logger.info(f"iteration 0 Answer: {str(p_ans)}")
            pass

        if self.task.completed is None:
            raise ValueError("this task does not define a completion function")
        iteration = 0

        while True:
            evaluation_answer = cast(AnswerCompletion, self.task.completed)(question, latest_answer)
            eval_history.append(evaluation_answer)
            if verbose:
                # logger.info(f"eval comment from {iteration} editing: \n {str(evaluation_answer)}")
                pass

            if evaluation_answer if isinstance(evaluation_answer, bool) else evaluation_answer.success():
                break

            iteration += 1
            if verbose:
                logger.info(f"start iteration {iteration} editing")
            next_question = self.task.next_question(questions_history, answers_history, eval_history)
            p_ans, ans, meta, p = self.rough_guess(next_question)

            answers_history.append(p_ans)
            latest_answer = p_ans
            if verbose:
                # logger.info(f"iteration {iteration} editing output: \n{str(p_ans)}")
                pass

        if verbose:
            # logger.info(f"Returning answer at iteration {iteration}: \n{str(p_ans)}")
            pass
        return latest_answer, ans, meta, p



    def many_rough_guesses(self, num_threads:int,
                           question:Question, max_tokens: int = 1000,
                           verbose: bool = False, max_tries: int = 1) -> BackendManyResults:
        """
        Args:
            num_threads : number of independent threads.
            all other  arguments are same as those of `rough_guess()`

        Returns
            List of elements, each element is a tuple following the
            return signature of `rough_guess()`
        """

        p = self.prepare_payload(question, verbose=verbose, prepend=None,
                                    model=self.model,
                                    max_tokens=max_tokens)

        n_choices = num_threads

        reattempt = 0
        while True:
            response, meta_data = self.ask(p, n_choices=n_choices)
            try:
                parsed_response = [
                    attach_response_metadata(
                        parse_response(self.task, r),
                        response_metadata=r,
                        request_payload=p,
                    )
                    for r in response
                ]
            except GPTOutputParseException:
                # logger.warning(f"The following was not parseable:\n\n{response}\n\nBecause\n\n{e}")
                pass

                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p


    def rough_guess(self, question:Question, max_tokens: int = 1000, verbose: bool = False,
                    max_tries: int = 1, query_id:int=0) -> BackendResult:
        """
        Args:
            question
            max_tokens (int) : max tokens in return from
            verbose (bool)
        Returns:
            answer in the form of ParsedAnswer
            answer in the form of raw text response from LLM
            meta data of the response
            json payload sent to the LLM
        """

        p = self.prepare_payload(question, verbose=verbose, prepend=None,
                                    model=self.model,
                                    max_tokens=max_tokens)

        print('Loading payload fine.')

        reattempt = 0
        while True:
            responses, meta_data = self.ask(p)
            response = responses[0]
            try:
                parsed_response = attach_response_metadata(
                    parse_response(self.task, response),
                    response_metadata=response,
                    request_payload=p,
                )
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
