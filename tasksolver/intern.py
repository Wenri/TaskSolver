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
from .hf_types import InternPayload
from .hf_types import InternRemoteModel
from PIL import Image
from importlib import import_module
from typing import cast
from .hf_types import RemoteTransformersSDK

AutoModel = cast(RemoteTransformersSDK[InternRemoteModel], import_module("transformers")).AutoModel
from collections.abc import Callable, Sequence
from .hf_types import Tensor, TransformsSDK, GenerationConfig

seed = 42
torch.manual_seed(seed)
np.random.seed(seed)
random.seed(seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

def build_transform(input_size: int) -> Callable[[Image.Image], Tensor]:
    transforms = cast(TransformsSDK, import_module("torchvision.transforms"))
    def rgb(image: Image.Image) -> Image.Image:
        return image.convert("RGB") if image.mode != "RGB" else image
    return transforms.Compose([transforms.Lambda(rgb), transforms.ToTensor()])

def find_closest_aspect_ratio(aspect_ratio: float, target_ratios: Sequence[tuple[int, int]],
                              width: int, height: int, image_size: int) -> tuple[int, int]:
    best_ratio_diff = float('inf')
    best_ratio = (1, 1)
    area = width * height
    for ratio in target_ratios:
        target_aspect_ratio = ratio[0] / ratio[1]
        ratio_diff = abs(aspect_ratio - target_aspect_ratio)
        if ratio_diff < best_ratio_diff:
            best_ratio_diff = ratio_diff
            best_ratio = ratio
        elif ratio_diff == best_ratio_diff:
            if area > 0.5 * image_size * image_size * ratio[0] * ratio[1]:
                best_ratio = ratio
    return best_ratio

def dynamic_preprocess(image: Image.Image, min_num: int = 1, max_num: int = 12,
                       image_size: int = 448, use_thumbnail: bool = False) -> list[Image.Image]:
    orig_width, orig_height = image.size
    aspect_ratio = orig_width / orig_height

    # calculate the existing image aspect ratio
    ratio_set = set(
        (i, j) for n in range(min_num, max_num + 1) for i in range(1, n + 1) for j in range(1, n + 1) if
        i * j <= max_num and i * j >= min_num)
    target_ratios = sorted(ratio_set, key=lambda x: x[0] * x[1])

    # find the closest aspect ratio to the target
    target_aspect_ratio = find_closest_aspect_ratio(
        aspect_ratio, target_ratios, orig_width, orig_height, image_size)

    # calculate the target width and height
    target_width = image_size * target_aspect_ratio[0]
    target_height = image_size * target_aspect_ratio[1]
    blocks = target_aspect_ratio[0] * target_aspect_ratio[1]

    # resize the image
    resized_img = image.resize((target_width, target_height))
    processed_images: list[Image.Image] = []
    for i in range(blocks):
        box = (
            (i % (target_width // image_size)) * image_size,
            (i // (target_width // image_size)) * image_size,
            ((i % (target_width // image_size)) + 1) * image_size,
            ((i // (target_width // image_size)) + 1) * image_size
        )
        # split the image
        split_img = resized_img.crop(box)
        processed_images.append(split_img)
    assert len(processed_images) == blocks
    if use_thumbnail and len(processed_images) != 1:
        thumbnail_img = image.resize((image_size, image_size))
        processed_images.append(thumbnail_img)
    return processed_images

def load_image(image: Image.Image, input_size: int = 448, max_num: int = 12) -> Tensor:
    image = image.convert('RGB')
    transform = build_transform(input_size=input_size)
    images = dynamic_preprocess(image, image_size=input_size, use_thumbnail=True, max_num=max_num)
    return torch.stack([transform(image) for image in images])

class InternModel(object):
    def __init__(self, task:TaskSpec,
                 model:str="OpenGVLab/InternVL2-8B"):

        self.task:TaskSpec = task
        self.model = self.get_model(model)

        self.tokenizer = transformers.AutoTokenizer.from_pretrained(model, trust_remote_code=True, use_fast=False)


    def get_model(self, model: str) -> InternRemoteModel:
        # Load the open-source model in

        if model == "OpenGVLab/InternVL2-8B":
            model_weights = AutoModel.from_pretrained(
                model,
                torch_dtype=torch.float16,
                load_in_4bit=True,
                low_cpu_mem_usage=True,
                use_flash_attn=True,
                trust_remote_code=True).eval()
            return model_weights
        else:
            raise ValueError(f"Such model {model} does not exist!")

    def ask(self, payload: InternPayload, n_choices: int = 1, temperature: float = 0.7) -> tuple[list[JSONObject], list[object]]:
        """
        args:
            payload: json dictionary, prepared by `prepare_payload`
        """

        def intern_thread(self: InternModel, idx: int, payload: InternPayload, results: list[HFResult | None], temperature: float) -> None:

            # creation of payload
            question = payload['question']
            pixel_values = payload['pixel_values']
            num_patches_list = payload['num_patches_list']
            max_tokens = payload['max_tokens']

            generation_config: GenerationConfig = {"max_new_tokens": max_tokens, "do_sample": True}

            try:
                # Preparation for inference
                output_text = self.model.chat(self.tokenizer, pixel_values, question, generation_config, num_patches_list=num_patches_list, history=None, return_history=None)

            except Exception as e:
                raise e

            # print('outputs: ', output_text)
            message: JSONObject = {'content': output_text}

            results[idx] = {"metadata": output_text, "message": message}

            return

        assert n_choices >= 1
        results: list[HFResult | None] = [None] * n_choices
        if n_choices > 1:
            intern_jobs = [threading.Thread(target=intern_thread,
                            args=(self, idx, payload, results, temperature))
                                for idx in range(n_choices)]
            for job in intern_jobs:
                job.start()
            for job in intern_jobs:
                job.join()
        else:
            intern_thread(self, 0, payload, results, temperature)

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
            ) -> InternPayload:

        text = ''
        img_list: list[Image.Image | None] = []
        for dic in question.get_json():
            # The case of text
            if dic['type'] == 'text':
                text += dic['text']

            # The case of vision input
            elif dic['type'] == 'image_url':
                img_list.append(dic['image'])
                text += '<image>\n'

        pixel_list: list[Tensor] = []
        for image in img_list:
            if image is None:
                raise ValueError("Intern requires a loaded image")
            pixel_list.append(load_image(image).to(torch.float16).cuda())

        if pixel_list:
            pixel_values = torch.cat(tuple(pixel_list), dim=0)
            num_patches_list = [img_tensor.size(0) for img_tensor in pixel_list]

        else:
            pixel_values = None
            num_patches_list = None

        payload: InternPayload = {
            'question': text,
            'pixel_values': pixel_values,
            'num_patches_list':num_patches_list,
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
                # logger.warning(f"The following was not parseable:\n\n{response}\n\nBecause\n\n{e}")
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

                logger.warning(f"Reattempt #{reattempt} querying LLM")
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

    def run_once(self, question:Question, max_tokens: int = 1000, temperature: float = 1, *, max_tries: int = 1, query_id: int = 0, verbose: bool = False, **kwargs: object) -> BackendResult:
        q = self.task.first_question(question)
        p_ans, ans, meta, p = self.rough_guess(q, max_tokens=max_tokens, temperature=temperature, max_tries=max_tries, query_id=query_id, verbose=verbose, **kwargs)
        return p_ans, ans, meta, p
