from __future__ import annotations

import random
import threading
import numpy as np
from loguru import logger
from .common import TaskSpec, Question
from .exceptions import GPTOutputParseException, GPTMaxTriesExceededException
from .provider_types import BackendResult, BackendManyResults, parse_response
from .hf_types import HFResult, completed_results, torch, transformers
from wirecap.types import JSONObject
from .hf_types import TextPayload

seed = 42
torch.manual_seed(seed)
np.random.seed(seed)
random.seed(seed)
torch.backends.cudnn.benchmark = False



class LlamaModel(object):
    def __init__(self, task:TaskSpec,
                 model:str = "meta-llama/Meta-Llama-3.1-8B-Instruct"):

        self.task:TaskSpec = task
        self.model_id = model

        num_gpus = torch.cuda.device_count()

        if num_gpus not in (1, 2):
            raise ValueError("this Llama adapter requires one or two CUDA devices")

        # Assign device based on the available GPUs
        if num_gpus == 2:
            self.device_map = "cuda:1"
        if num_gpus == 1:
            self.device_map = "cuda:0"

        self.pipeline = transformers.pipeline(
            "text-generation",
            model=self.model_id,
            model_kwargs={"attn_implementation":"flash_attention_2", "torch_dtype": torch.float16},
            device_map=self.device_map,  # Map the model to GPU 2 (index 1)
        )

        # self.pipeline = transformers.pipeline(
        #     "text-generation",
        #     model=self.model_id,
        #     model_kwargs={"torch_dtype": torch.bfloat16},
            # device_map="auto",  # Map the model to GPU 2 (index 1)
        # )

    def ask(self, payload: TextPayload, n_choices: int = 1, temperature: float = 0.7) -> tuple[list[JSONObject], list[object]]:
        """
        args:
            payload: json dictionary, prepared by `prepare_payload`
        """

        def llama_thread(self: LlamaModel, idx: int, payload: TextPayload, results: list[HFResult | None], temperature: float) -> None:

            # creation of payload
            messages = payload['messages']
            max_tokens = payload['max_tokens']


            try:
                with torch.autocast(device_type='cuda'):
                    output_text = self.pipeline(
                        messages,
                        max_new_tokens=max_tokens,
                    )

            except Exception as e:
                raise e

            # print('outputs: ', output_text)
            message = output_text[0]["generated_text"][-1]

            results[idx] = {"metadata": output_text, "message": message}

            return

        assert n_choices >= 1
        results: list[HFResult | None] = [None] * n_choices
        if n_choices > 1:
            llama_jobs = [threading.Thread(target=llama_thread,
                            args=(self, idx, payload, results, temperature))
                                for idx in range(n_choices)]
            for job in llama_jobs:
                job.start()
            for job in llama_jobs:
                job.join()
        else:
            llama_thread(self, 0, payload, results, temperature)

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
            ) -> TextPayload:

        text = ''
        for dic in question.get_json():
            # The case of text
            if dic['type'] == 'text':
                text += dic['text']

            # The case of vision input


        payload: TextPayload = {
            "messages": [
                {"role": "user", "content": text},
            ],
            "max_tokens": max_tokens,
        }


        return payload


    def rough_guess(self, question:Question, max_tokens: int = 1000,
                    max_tries: int = 1, query_id:int=0,
                    verbose: bool = False, temperature: float = 1,
                    **kwargs: object) -> BackendResult:

        p = self.prepare_payload(question, max_tokens = max_tokens, verbose=verbose, prepend=None)

        reattempt = 0
        while True:
            responses, meta_data = self.ask(p, temperature=temperature)
            response = responses[0]
            # logger.info(f'response: {response}')
            try:
                parsed_response = parse_response(self.task, response)
            except GPTOutputParseException:
                # logger.warning(f"The following was not parseable:\n\n{response}\n\nBecause\n\n{e}")

                # if not os.path.exists('errors/'):
                #     # Create the directory if it doesn't exist
                #     os.makedirs('errors/')
                # error_saved = f'errors/{time.strftime("%Y-%m-%d-%H-%M-%S")}.json'
                # with open(error_saved, "w")  as f:
                #     f.write(p_ans.code)
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
                           verbose: bool = False, max_tries: int = 1, temperature: float = 1
                           ) -> BackendManyResults:
        """
        Args:
            num_threads : number of independent threads.
            all other  arguments are same as those of `rough_guess()`

        Returns
            List of elements, each element is a tuple following the
            return signature of `rough_guess()`
        """

        p = self.prepare_payload(question, max_tokens = max_tokens, verbose=verbose, prepend=None)

        #  TODO
        n_choices = num_threads

        # TODO: wrap in robust-ask method, repeatedly asks until parseable output.
        reattempt = 0
        while True:
            response, meta_data = self.ask(p, n_choices=n_choices, temperature=temperature)
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

    def run_once(self, question:Question, max_tokens: int = 1000, temperature: float = 1, *, max_tries: int = 1, query_id: int = 0, verbose: bool = False, **kwargs: object) -> BackendResult:
        q = self.task.first_question(question)
        p_ans, ans, meta, p = self.rough_guess(q, max_tokens=max_tokens, temperature=temperature, max_tries=max_tries, query_id=query_id, verbose=verbose, **kwargs)
        return p_ans, ans, meta, p
