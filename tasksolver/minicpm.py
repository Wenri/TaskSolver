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
from .hf_types import MiniCPMPayload
from .hf_types import MiniCPMRemoteModel
from PIL import Image
from importlib import import_module
from typing import cast
from .hf_types import RemoteTransformersSDK

AutoModel = cast(RemoteTransformersSDK[MiniCPMRemoteModel], import_module("transformers")).AutoModel

seed = 42
torch.manual_seed(seed)
np.random.seed(seed)
random.seed(seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False



class MiniCPMModel(object):
    def __init__(self, task:TaskSpec,
                 model:str = "openbmb/MiniCPM-V-2_6-int4"):

        self.task:TaskSpec = task
        self.model = self.get_model(model)

        self.tokenizer = transformers.AutoTokenizer.from_pretrained('openbmb/MiniCPM-V-2_6-int4', trust_remote_code=True)


    def get_model(self, model: str) -> MiniCPMRemoteModel:
        # Load the open-source model in

        if model == 'openbmb/MiniCPM-V-2_6-int4':
            model_weights = AutoModel.from_pretrained(model, trust_remote_code=True,
            attn_implementation='flash_attention_2', torch_dtype=torch.float16) # sdpa or flash_attention_2, no eager
            model_weights = model_weights.eval()

            return model_weights

        else:
            raise ValueError(f"Such model {model} does not exist!")

    def ask(self, payload: MiniCPMPayload, n_choices: int = 1, temperature: float = 0.7) -> tuple[list[JSONObject], list[object]]:
        """
        args:
            payload: json dictionary, prepared by `prepare_payload`
        """

        def minicpm_thread(self: MiniCPMModel, idx: int, payload: MiniCPMPayload, results: list[HFResult | None], temperature: float) -> None:

            # creation of payload
            messages = payload['messages']


            try:
                # Preparation for inference
                output_text = self.model.chat(
                    image=None,
                    msgs=messages,
                    tokenizer=self.tokenizer
                )
            except Exception as e:
                raise e

            # print('outputs: ', output_text)
            message: JSONObject = {'content': output_text}

            results[idx] = {"metadata": output_text, "message": message}

            return

        assert n_choices >= 1
        results: list[HFResult | None] = [None] * n_choices
        if n_choices > 1:
            minicpm_jobs = [threading.Thread(target=minicpm_thread,
                            args=(self, idx, payload, results, temperature))
                                for idx in range(n_choices)]
            for job in minicpm_jobs:
                job.start()
            for job in minicpm_jobs:
                job.join()
        else:
            minicpm_thread(self, 0, payload, results, temperature)

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
            ) -> MiniCPMPayload:

        text = ''
        img_list: list[Image.Image | None] = []
        for dic in question.get_json():
            # The case of text
            if dic['type'] == 'text':
                text += dic['text']

            # The case of vision input
            elif dic['type'] == 'image_url':
                img_list.append(dic['image'])

        if len(img_list) == 0:
            img_list.append(Image.new('RGB', (512, 512), color = (255, 255, 255)))

        content: list[Image.Image | str | None] = list(img_list)
        content.append(text)

        payload: MiniCPMPayload = {
            "messages": [
                {
                    'role': 'user',
                    "content":content,
                },
            ],
            "max_tokens": max_tokens,
        }


        return payload


    def rough_guess(self, question:Question, max_tokens: int = 1000,
                    max_tries: int = 1, query_id:int=0,
                    verbose: bool = False, temperature: float = 1,
                    **kwargs: object) -> BackendResult:

        p = self.prepare_payload(question, max_tokens = max_tokens, verbose=verbose, prepend=None,
                                    model=self.model)

        reattempt = 0
        while True:
            responses, meta_data = self.ask(p, temperature=temperature)
            response = responses[0]
            # logger.info(f'response: {response}')
            try:
                parsed_response = parse_response(self.task, response)
            except GPTOutputParseException:
                # logger.warning(f"The response was not parseable:\n\n{response}\n\nBecause\n\n{e}")
                # logger.warning(f"The response from LM was not parseable.")
                pass

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

                logger.warning(f"Attempt failed: Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p

    def all_task_rough_guess(self, task: TaskSpec, question:Question, max_tokens: int = 1000,
                    max_tries: int = 1, query_id:int=0,
                    verbose: bool = False, temperature: float = 1,
                    **kwargs: object) -> BackendResult:

        p = self.prepare_payload(question, max_tokens = max_tokens, verbose=verbose, prepend=None,
                                    model=self.model)

        reattempt = 0
        while True:
            responses, meta_data = self.ask(p, temperature=temperature)
            response = responses[0]
            # logger.info(f'response: {response}')
            try:
                parsed_response = parse_response(task, response)
            except GPTOutputParseException:
                # logger.warning(f"The following was not parseable:\n\n{response}\n\nBecause\n\n{e}")
                # logger.warning(f"The response from LM was not parseable.")
                pass

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

                logger.warning(f"Attempt failed: reattempt #{reattempt} querying LLM")
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

        p = self.prepare_payload(question, max_tokens = max_tokens, verbose=verbose, prepend=None,
                                    model=self.model)

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
                # logger.warning(f"The response from LM was not parseable.")
                pass

                # TODO provide the parse error message into GPT for the next round to be parsable
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Attempt failed: Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p

    def run_once(self, question:Question, max_tokens: int = 1000, temperature: float = 1, *, max_tries: int = 1, query_id: int = 0, verbose: bool = False, **kwargs: object) -> BackendResult:
        q = self.task.first_question(question)
        p_ans, ans, meta, p = self.rough_guess(q, max_tokens=max_tokens, temperature=temperature, max_tries=max_tries, query_id=query_id, verbose=verbose, **kwargs)
        return p_ans, ans, meta, p
