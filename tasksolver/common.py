from __future__ import annotations

import base64
import io
import os
import threading
from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, Self, TypedDict, cast, runtime_checkable

from bson import ObjectId
from loguru import logger
from PIL import Image
import requests

from .utils import URL

if TYPE_CHECKING:
    from .agent import Agent


class TextContent(TypedDict):
    type: Literal["text"]
    text: str


class ImageURL(TypedDict):
    url: str


class ImageContent(TypedDict):
    type: Literal["image_url"]
    image_url: ImageURL
    image: Image.Image | None
    local_path: NotRequired[str]


type QuestionContent = TextContent | ImageContent
type QuestionComponent = URL | Path | str | ParsedAnswer | Image.Image
type QuestionTag = tuple[str, ...] | None
type TaggedComponent = tuple[QuestionComponent, QuestionTag]
type ResponseMetadata = dict[str, object]


@runtime_checkable
class CompletionAnswer(Protocol):
    def success(self) -> bool: ...


type AnswerCompletion = Callable[[Question, ParsedAnswer], bool | CompletionAnswer]
type AgentCompletion = Callable[[Agent], tuple[Question, ParsedAnswer]]


class TaskExample(TypedDict):
    question: Question
    answer: ParsedAnswer
    explanation: str | None


io_semaphore = threading.Semaphore(1)

#: Where ``Question.get_pil_image_content_savecopy`` writes images when the caller names no
#: directory. It used to be the CWD-relative ``temporary/``: that polluted the caller's working
#: tree, and an agent CLI running in a different workspace could not resolve the relative path.
IMAGE_DIR_ENV = "TASKSOLVER_IMAGE_DIR"


def image_media_type(data: bytes, default: str = "image/jpeg") -> str:
    """The media type of encoded image bytes, from their signature (``default`` if unknown)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return default


def default_image_dir() -> str:
    import tempfile
    return os.environ.get(IMAGE_DIR_ENV) or os.path.join(tempfile.gettempdir(), "tasksolver-images")


def _stringify_reasoning_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, list):
        parts: list[str] = []
        for item in cast(list[object], value):
            if isinstance(item, Mapping):
                item = cast(Mapping[object, object], item)
                text = item.get("text")
                if text:
                    parts.append(str(text).strip())
                elif item.get("type") == "thinking" and item.get("thinking"):
                    parts.append(str(item.get("thinking")).strip())
            else:
                text = _stringify_reasoning_value(item)
                if text:
                    parts.append(text)
        combined = "\n\n".join(part for part in parts if part)
        return combined or None
    if isinstance(value, Mapping):
        value = cast(Mapping[object, object], value)
        for key in ("text", "thinking", "content", "reasoning_content", "reasoning"):
            if key in value:
                text = _stringify_reasoning_value(value.get(key))
                if text:
                    return text
        return None
    return str(value).strip() or None


def extract_explicit_reasoning_output(response_metadata: object) -> str | None:
    if response_metadata is None:
        return None

    if isinstance(response_metadata, list):
        outputs: list[str] = []
        for item in cast(list[object], response_metadata):
            text = extract_explicit_reasoning_output(item)
            if text:
                outputs.append(text)
        combined = "\n\n".join(outputs)
        return combined or None

    if not isinstance(response_metadata, Mapping):
        return None

    response_metadata = cast(Mapping[object, object], response_metadata)
    if response_metadata.get("explicit_reasoning_output"):
        return _stringify_reasoning_value(response_metadata.get("explicit_reasoning_output"))

    for key in ("reasoning_content", "reasoning", "thinking"):
        text = _stringify_reasoning_value(response_metadata.get(key))
        if text:
            return text

    content = response_metadata.get("content")
    if isinstance(content, list):
        thinking_blocks: list[str] = []
        for block in cast(list[object], content):
            if not isinstance(block, Mapping):
                continue
            mapping = cast(Mapping[object, object], block)
            if mapping.get("type") == "thinking":
                text = _stringify_reasoning_value(mapping.get("thinking"))
                if text:
                    thinking_blocks.append(text)
        if thinking_blocks:
            return "\n\n".join(thinking_blocks)

    return None


def attach_response_metadata[T: ParsedAnswer](
    parsed_response: T, response_metadata: object = None, request_payload: object = None,
) -> T:
    parsed_response.llm_response_metadata = response_metadata
    parsed_response.request_payload = request_payload
    parsed_response.explicit_reasoning_output = extract_explicit_reasoning_output(response_metadata)
    return parsed_response

class ParsedAnswer(object):
    """ Base class to specify parsing output types
    Needs to be specified PER task
    """
    llm_response_metadata: object
    request_payload: object
    explicit_reasoning_output: str | None

    def __init__(self) -> None:
        pass

    @staticmethod
    @abstractmethod
    def parser(gpt_raw: str) -> ParsedAnswer:
        raise NotImplementedError

    @abstractmethod
    def __str__(self) -> str:
        raise NotImplementedError


class Question(object):
    def __init__(self, elements: Sequence[object] | None):
        self.elements: list[TaggedComponent] = []
        for element in elements or ():
            tag: QuestionTag = None
            component: object = element
            if isinstance(element, (tuple, list)):
                pair = cast(Sequence[object], element)
                assert len(pair) == 2
                component, raw_tag = pair
                if isinstance(raw_tag, str):
                    tag = (raw_tag,)
                elif raw_tag is not None:
                    if not isinstance(raw_tag, (tuple, list)):
                        raise TypeError("question tags must be strings or tuples of strings")
                    values = cast(tuple[object, ...] | list[object], raw_tag)
                    if not all(isinstance(value, str) for value in values):
                        raise TypeError("question tags must contain strings")
                    tag = tuple(cast(Sequence[str], values))
            if isinstance(component, Question):
                for nested, nested_tag in component.eval():
                    self.elements.append((nested, (nested_tag or ()) + tag if tag is not None
                                          else nested_tag))
            elif isinstance(component, (URL, Path, str, ParsedAnswer, Image.Image)):
                self.elements.append((component, tag))
            else:
                raise ValueError(f"invalid element type {type(component)} in question input!")

    @staticmethod
    def encode_image(image_path: Path | str) -> str:
        with open(str(image_path), "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')

    @staticmethod
    def get_text_content(text: str) -> TextContent:
        return {"type": "text",
                "text": text}


    @staticmethod
    def get_pil_image_content(image: Image.Image) -> ImageContent:
        with io_semaphore:
            img_byte_array = io.BytesIO()
            image.save(img_byte_array, format='PNG')  # Save the PIL image to the in-memory stream as PNG
            img_byte_array.seek(0)
            base64enc_image = base64.b64encode(img_byte_array.read()).decode('utf-8')
            # the bytes are PNG (above); the data URL used to claim image/jpeg
            pack: ImageContent = {"type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{base64enc_image}"
                    },
                "image": image
                }
        return pack

    @staticmethod
    def get_local_image_content(image_path: Path | str) -> ImageContent:
        with open(str(image_path), "rb") as image_file:
            data = image_file.read()
        base64enc_image = base64.b64encode(data).decode('utf-8')
        image = Image.open(io.BytesIO(data))
        image.load()   # decode now: the lazily opened file used to stay open
        return {"type": "image_url",
                "image_url": {
                    "url": f"data:{image_media_type(data)};base64,{base64enc_image}"
                    },
                "image": image
                }

    @staticmethod
    def get_pil_image_content_savecopy(image: Image.Image, directory: str | Path | None = None) -> ImageContent:
        """Save ``image`` as a PNG under ``directory`` (default: :func:`default_image_dir`) and
        return its content dict with the ABSOLUTE ``local_path``.

        PNG, not the former JPEG: lossless, so thin annotations (1-px markers, line art) reach a
        file-reading agent exactly as they reach the inline-image backends."""
        directory = os.path.abspath(directory or default_image_dir())
        os.makedirs(directory, exist_ok=True)
        filepath = os.path.join(directory, f"{ObjectId()}.png")
        image.save(filepath, format="PNG")
        ret = Question.get_local_image_content(filepath)
        ret["local_path"] = filepath
        return ret

    @staticmethod
    def get_remote_image_content(image_url: URL) -> ImageContent:
        return {"type": "image_url",
                "image_url": {
                    "url": str(image_url),
                    },
                "image": None # we don't pre-read the image from the url.
                }

    def __str__(self) -> str:
        return "\n".join([str(el[0]) for el in self.elements] )

    def prepend_question(self, other_question: Question) -> Self:
        self.elements = other_question.eval() + self.elements
        return self

    def append_question(self, other_question: Question) -> Self:
        self.elements = self.elements + other_question.eval()
        return self

    def __add__(self, other: Question) -> Question:
        return Question(self.elements).append_question(other)

    def subquestion(self, filter_tag: tuple[str, ...] | str | None) -> Question:
        return Question(self.eval(filter_tag=filter_tag))

    def eval(self, filter_tag: tuple[str, ...] | str | None = None) -> list[TaggedComponent]:
        """ Returns all the question components, and disregards tags
        Args:
            filter_tag: if None, then return everything. Otherwise, return the components that match the
                tags found in filter_tag.
        """
        if isinstance(filter_tag , str):
            filter_tag = (filter_tag,)
        return_elements: list[TaggedComponent] = []
        for comp, tag in self.elements:
            # NOTE: tag is a tuple or it is None.
            if filter_tag is None:
                # all should be allowed
                return_elements.append((comp, tag))
            elif tag is not None and len(set(tag).intersection(set(filter_tag))) > 0:
                return_elements.append((comp, tag))
            # else, ignore.  (tag = None, or null set intersection)
        return return_elements

    @property
    def question_components(self) -> list[QuestionComponent]:
        return [el[0] for el in self.elements]

    def images(self) -> list[Image.Image]:
        """ Returns a list of all the images indicated in the Question object.
        """
        imgs: list[Image.Image] = []
        for el in self.eval():
            component = el[0]
            if isinstance(component, Image.Image):
                imgs.append(component)
            elif isinstance(component, Path):
                imgs.append(Image.open(component))
            elif isinstance(component, URL):
                try:
                    response= requests.get(str(component))
                    response.raise_for_status()
                    img = Image.open(io.BytesIO(response.content))
                    imgs.append(img)
                except requests.RequestException as e:
                    logger.warning(f"Error fetching the image from URL: {e}")
                    continue
        return imgs

    def get_json(self, *, save_local: bool = False, save_dir: str | Path | None = None,
                 **kwargs: object) -> list[QuestionContent]:
        """Provider-neutral content list. ``save_local=True`` also writes each PIL image to a
        PNG file (under ``save_dir``, default :func:`default_image_dir`) for CLI agents that
        read images off disk; the entry then carries its absolute ``local_path``."""
        payload: list[QuestionContent] = []
        for el in self.question_components:
            if isinstance(el, str):
                payload.append(self.get_text_content(el))
            elif isinstance(el, Image.Image):
                if save_local is True:
                    payload.append(self.get_pil_image_content_savecopy(el, save_dir))
                else:
                    payload.append(self.get_pil_image_content(el))
            elif isinstance(el, Path):
                payload.append(self.get_local_image_content(el))
            elif isinstance(el, URL):
                payload.append(self.get_remote_image_content(el))
            else:
                payload.append(self.get_text_content(str(el)))
        return payload


class TaskSpec(object):
    """ Specifies the task

    Example usage:
        TS = TaskSpec(name="human detector",
            description="Return the bounding box parameters (lower left, top right) pixel coordinates that circumscribe instances of humans within the input image.")

        # add examples
        TS.add_example(
            input=Question(...), # can include images
            output=ParsedAnswer(...),
            explanation="Because ..." # or None
        )

        # tell it the steps you'd like it to go through to evaluate the problem
        # e.g. takes the input Question, takes the image, and then right-concatenates
        # that with the visualization output of the previous step
    """
    def __init__(self,
                 name:str,
                 description:str,
                 answer_type: type[ParsedAnswer],
                 followup_func: Callable[[TaskSpec, list[Question], list[ParsedAnswer], list[bool | CompletionAnswer]], Question] | None,
                 completed_func: AnswerCompletion | AgentCompletion | None,
                 ) -> None:
        """
        Args:
            followup_func: is a callable that learns maps from the input question, parsed answer
                and returns another question to ask to the model.
        """
        self.name = name
        self.description = description

        self.followup_func = followup_func  # custom function, needs to be specified PER Task
        self.completed = completed_func
        self.answer_type = answer_type

        self.examples: list[TaskExample] = []
        self.background: Question | None = None

    def add_background(self, background: Question) -> None:
        self.background = background

    def add_example(self, input: Question, output: ParsedAnswer, explanation: str | None = None) -> Self:
        """ Used to add examples of I/O to the model.
        """
        self.examples.append({"question": input,
                              "answer": output,
                              "explanation": explanation})
        return self

    def task_question_component(self, filter_tag: tuple[str, ...] | str | None = None) -> Question:
        return Question([("# Task Description", "TASK_DESC_TITLE"),
                            (self.description, "TASK_DESC_CONTENT")]).subquestion(filter_tag=filter_tag)
    def background_question_component(self, filter_tag: tuple[str, ...] | str | None = None) -> Question:
        assert isinstance(self.background, Question)
        question = Question([("# Background information", "BACKGROUND_TITLE"),
                            (self.background, "BACKGROUND_CONTENT")])
        return question.subquestion(filter_tag=filter_tag)

    def example_question_component(self, filter_tag: tuple[str, ...] | str | None = None) -> Question:
        question = Question([])
        question.append_question(Question([("# Examples", "EXAMPLES_TITLE")]))
        question.append_question(Question([(f"Here are {len(self.examples)} examples:", "EXAMPLES_CONTENT")]))
        for ex_idx, ex_dict in enumerate(self.examples):

            question.append_question(Question([(f"(Ex #{ex_idx}) Question:", ("EXAMPLES_QUESTION_TITLE", f"EXAMPLE_{ex_idx}"))]))
            question.append_question(Question([(ex_dict["question"], ("EXAMPLES_QUESTION_CONTENT", f"EXAMPLE_{ex_idx}"))])) # the question

            if ex_dict["explanation"] is not None:
                question.append_question(Question([(f"(Ex #{ex_idx}) Reasoning:", ("EXAMPLES_REASON_TITLE", f"EXAMPLE_{ex_idx}"))]))
                question.append_question(Question([(ex_dict["explanation"], ("EXAMPLES_REASON_CONTENT", f"EXAMPLE_{ex_idx}"))]))

            question.append_question(Question([(f"(Ex #{ex_idx}) Answer:", ("EXAMPLES_ANSWER_TITLE", f"EXAMPLE_{ex_idx}"))]))
            question.append_question(Question([(str(ex_dict["answer"]), ("EXAMPLES_ANSWER_CONTENT", f"EXAMPLE_{ex_idx}"))]))
            question.append_question(Question(["\n\n"]))
        return question.subquestion(filter_tag=filter_tag)

    def prompt_question_component(self, user_question: Question, filter_tag: tuple[str, ...] | str | None = None) -> Question:
        question = Question([])
        question.append_question(Question([("# Your turn -- please complete the following task:", "QUESTION_TITLE")]))
        question.append_question(Question([(user_question, "QUESTION_CONTENT")]))
        return question.subquestion(filter_tag=filter_tag)

    def first_question(self, question: Question) -> Question:
        # task description
        first_q = self.task_question_component()
        # background information
        if self.background is not None:
            first_q.append_question(self.background_question_component())
        # examples
        if len(self.examples) > 0:
            first_q.append_question(self.example_question_component())
        # finally, the question
        first_q.append_question(self.prompt_question_component(question))

        return first_q

    def next_question(self, questions_history:list[Question],
                            answers_history:list[ParsedAnswer],
                            eval_history: list[bool | CompletionAnswer]) -> Question:
        if self.followup_func is None:
            raise ValueError("this task does not define a follow-up function")
        next_q = self.followup_func(self, questions_history, answers_history, eval_history)
        return next_q


class KeyChain(object):
    def __init__(self, keys: dict[str, str] | None = None) -> None:
        if keys is None:
            self.keys: dict[str, str] = {}
        else:
            self.keys = keys

    def add_key(self, service: str, key: str) -> Self:
        if os.path.exists(key): # it's a file
            with open(key, "r") as f:
                key = f.readline().strip()
        self.keys.update({service: key})
        return self

    def get_key(self, service: str) -> str:
        if service not in self.keys:
            raise ValueError(f"No keys associated with '{service}'")

        return self.keys[service]

    def __getitem__(self, service: str) -> str:
        return self.get_key(service)
