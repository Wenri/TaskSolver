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
from .hf_types import QwenPayload
from .hf_types import GenerateModel
from PIL import Image
from importlib import import_module
from typing import cast
import time
from .common import TextContent
from .hf_types import VisionPart, VisionSDK

process_vision_info = cast(VisionSDK, import_module("qwen_vl_utils")).process_vision_info

seed = 42
torch.manual_seed(seed)
np.random.seed(seed)
random.seed(seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False



class QwenModel(object):
    def __init__(self, task:TaskSpec,
                 model:str = "Qwen/Qwen2-VL-7B-Instruct-AWQ"):

        self.task:TaskSpec = task
        self.model_name = model
        self.model = self.get_model(model)

        self.processor = transformers.AutoProcessor.from_pretrained("Qwen/Qwen2-VL-7B-Instruct-AWQ")

    def get_model(self, model: str) -> GenerateModel:
        # Load the open-source model in

        if model == 'Qwen/Qwen2-VL-7B-Instruct-AWQ':
            # model_weights = transformers.Qwen2VLForConditionalGeneration.from_pretrained(
            #     "Qwen/Qwen2-VL-7B-Instruct-AWQ", attn_implementation="flash_attention_2", torch_dtype=torch.float16, device_map="cuda:0"
            # )

            model_weights = transformers.Qwen2VLForConditionalGeneration.from_pretrained(
                "Qwen/Qwen2-VL-7B-Instruct-AWQ", torch_dtype=torch.float16, device_map='cuda:0'
            )

            # model_weights = AutoModelForSeq2SeqLM.from_pretrained("Qwen/Qwen2-VL-7B-Instruct-AWQ")
            return model_weights
        else:
            raise ValueError(f"Such model {model} does not exist!")

    def ask(self, payload: QwenPayload, n_choices: int = 1, temperature: float = 0.7) -> tuple[list[JSONObject], list[object]]:
        """
        args:
            payload: json dictionary, prepared by `prepare_payload`
        """

        def qwen_thread(self: QwenModel, idx: int, payload: QwenPayload, results: list[HFResult | None], temperature: float) -> None:

            # creation of payload
            messages = payload['messages']
            max_tokens = payload['max_tokens']

            # print(f'messages: {messages}')
            print(f'self.model: {self.model_name}')

            try:
                # Preparation for inference

                start = time.perf_counter()
                text = self.processor.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
                print(f'text: {text}')
                print(f'max_tokens: {max_tokens}')
                image_inputs, video_inputs = process_vision_info(messages)
                inputs = self.processor(
                    text=[text],
                    images=image_inputs,
                    videos=video_inputs,
                    padding=True,
                    return_tensors="pt",
                )
                inputs = inputs.to("cuda:0")

                print(f"Inputs: {inputs['input_ids'].shape}")
                # Inference: Generation of the output
                generated_ids = self.model.generate(**inputs, max_new_tokens=200)
                print(f'generated_ids: {generated_ids.shape}')
                generated_ids_trimmed = [
                    out_ids[len(in_ids) :] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
                ]
                print(f'generated_ids_trimmed: {generated_ids_trimmed[0].shape}')

                output_text = self.processor.batch_decode(
                    generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
                )

                elapsed = time.perf_counter() - start
                print(f'Elapsed: {elapsed:.6f} s')

                # print(f'output_text: {output_text}')
            except Exception as e:
                raise e

            # print('outputs: ', output_text)
            message: JSONObject = {'content': output_text[0]}

            results[idx] = {"metadata": output_text, "message": message}

            return

        assert n_choices >= 1
        results: list[HFResult | None] = [None] * n_choices
        if n_choices > 1:
            qwen_jobs = [threading.Thread(target=qwen_thread,
                            args=(self, idx, payload, results, temperature))
                                for idx in range(n_choices)]
            for job in qwen_jobs:
                job.start()
            for job in qwen_jobs:
                job.join()
        else:
            qwen_thread(self, 0, payload, results, temperature)

        complete = completed_results(results)
        messages = [res["message"] for res in complete]
        metadata = [res["metadata"] for res in complete]
        return messages, metadata


    @staticmethod
    def prepare_payload(question:Question,
            max_tokens: int = 200,
            verbose:bool=False,
            prepend: object = None,
            **kwargs: object
            ) -> QwenPayload:

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

        dict_list: list[VisionPart | TextContent] = [{'type':'image', 'image':image} for image in img_list]
        dict_list.append({"type": "text", "text":text})

        payload: QwenPayload = {
            "messages": [
                {
                    'role': 'user',
                    "content":dict_list,
                },
            ],
            "max_tokens": max_tokens,
        }


        return payload


    def rough_guess(self, question:Question, max_tokens: int = 200,
                    max_tries: int = 1, query_id:int=0,
                    verbose: bool = False, temperature: float = 1,
                    **kwargs: object) -> BackendResult:

        print('Rough guess ready')

        p = self.prepare_payload(question, max_tokens = max_tokens, verbose=verbose, prepend=None,
                                    model=self.model)

        print('Payload ready')

        reattempt = 0
        while True:
            print('Here')
            responses, meta_data = self.ask(p, temperature=temperature)
            response = responses[0]
            # logger.info(f'response: {response}')
            print(f'{response}')

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

    def all_task_rough_guess(self, task: TaskSpec, question:Question, max_tokens: int = 200,
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
                           question:Question, max_tokens: int = 200,
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

                # TODO provide the parse error message into GPT for the next round to be parsable
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            break

        return parsed_response, response, meta_data, p

    def run_once(self, question:Question, max_tokens: int = 200, temperature: float = 1, *, max_tries: int = 1, query_id: int = 0, verbose: bool = False, **kwargs: object) -> BackendResult:
        q = self.task.first_question(question)
        p_ans, ans, meta, p = self.rough_guess(q, max_tokens=max_tokens, temperature=temperature, max_tries=max_tries, query_id=query_id, verbose=verbose, **kwargs)
        return p_ans, ans, meta, p
