"""The interfaces used from optional Torch and remote Hugging Face models.

These protocols cover the consumed APIs without importing the CUDA stack into the
core package or treating remote model objects as dynamically typed.
"""
from collections.abc import Callable, Iterator, KeysView, Sequence
from contextlib import AbstractContextManager
from importlib import import_module
from typing import Literal, Protocol, Self, TypedDict, cast, overload

from PIL import Image
from wirecap.types import JSONObject
from .common import TextContent


class Tensor(Protocol):
    @property
    def shape(self) -> tuple[int, ...]: ...
    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator[Tensor]: ...
    def __getitem__(self, key: int | slice | tuple[slice, slice]) -> Tensor: ...
    def to(self, device_or_dtype: object) -> Self: ...
    def cuda(self) -> Self: ...
    def size(self, dim: int) -> int: ...


class TensorInputs(Protocol):
    @property
    def input_ids(self) -> Tensor: ...
    def keys(self) -> KeysView[str]: ...
    def __getitem__(self, key: str) -> Tensor: ...
    def to(self, device: str) -> Self: ...


class Cudnn(Protocol):
    deterministic: bool
    benchmark: bool


class Backends(Protocol):
    cudnn: Cudnn


class CUDA(Protocol):
    def device_count(self) -> int: ...


class TorchSDK(Protocol):
    backends: Backends
    cuda: CUDA
    float16: object
    def manual_seed(self, seed: int) -> object: ...
    def stack(self, tensors: Sequence[Tensor]) -> Tensor: ...
    def cat(self, tensors: Sequence[Tensor], dim: int) -> Tensor: ...
    def autocast(self, *, device_type: str) -> AbstractContextManager[object]: ...


class TextMessage(TypedDict):
    role: str
    content: str


class VisionPart(TypedDict):
    type: Literal["image"]
    image: Image.Image | None


class VisionMessage(TypedDict):
    role: str
    content: list[VisionPart | TextContent]


class MiniCPMMessage(TypedDict):
    role: str
    content: list[Image.Image | str | None]


class TextPayload(TypedDict):
    messages: list[TextMessage]
    max_tokens: int


class PhiPayload(TextPayload):
    images: list[Image.Image]


class QwenPayload(TypedDict):
    messages: list[VisionMessage]
    max_tokens: int


class MiniCPMPayload(TypedDict):
    messages: list[MiniCPMMessage]
    max_tokens: int


class InternPayload(TypedDict):
    question: str
    pixel_values: Tensor | None
    num_patches_list: list[int] | None
    max_tokens: int


class Tokenizer(Protocol):
    @property
    def eos_token_id(self) -> int | None: ...
    def apply_chat_template(self, messages: list[TextMessage], *, tokenize: bool,
                            add_generation_prompt: bool) -> str: ...


class Processor(Protocol):
    tokenizer: Tokenizer
    def apply_chat_template(self, messages: list[VisionMessage], *, tokenize: bool,
                            add_generation_prompt: bool) -> str: ...
    @overload
    def __call__(self, text: str, images: list[Image.Image], *, return_tensors: str) -> TensorInputs: ...
    @overload
    def __call__(self, *, text: list[str], images: list[Image.Image] | None,
                 videos: list[Tensor] | None, padding: bool, return_tensors: str) -> TensorInputs: ...
    def batch_decode(self, tokens: Tensor | list[Tensor], *, skip_special_tokens: bool,
                     clean_up_tokenization_spaces: bool) -> list[str]: ...


class GenerateModel(Protocol):
    def generate(self, **kwargs: Tensor | int | float | None) -> Tensor: ...


class GenerationConfig(TypedDict):
    max_new_tokens: int
    do_sample: bool


class InternRemoteModel(Protocol):
    def eval(self) -> Self: ...
    def chat(self, tokenizer: Tokenizer, pixel_values: Tensor | None, question: str,
             generation_config: GenerationConfig, *, num_patches_list: list[int] | None,
             history: None, return_history: None) -> str: ...


class MiniCPMRemoteModel(Protocol):
    def eval(self) -> Self: ...
    def chat(self, *, image: None, msgs: list[MiniCPMMessage], tokenizer: Tokenizer) -> str: ...


class PretrainedLoader[ModelT](Protocol):
    def from_pretrained(self, model: str, *, trust_remote_code: bool = False,
                        attn_implementation: str | None = None, _attn_implementation: str | None = None,
                        torch_dtype: object = None, device_map: str | None = None,
                        load_in_4bit: bool = False, low_cpu_mem_usage: bool = False,
                        use_flash_attn: bool = False, use_fast: bool = True,
                        num_crops: int = 1) -> ModelT: ...


class PipelineOutput(TypedDict):
    generated_text: list[JSONObject]


class TextPipeline(Protocol):
    def __call__(self, messages: list[TextMessage], *, max_new_tokens: int) -> list[PipelineOutput]: ...


class TransformersSDK(Protocol):
    AutoTokenizer: PretrainedLoader[Tokenizer]
    AutoProcessor: PretrainedLoader[Processor]
    AutoModelForCausalLM: PretrainedLoader[GenerateModel]
    Qwen2VLForConditionalGeneration: PretrainedLoader[GenerateModel]
    def pipeline(self, task: str, *, model: str, model_kwargs: dict[str, object],
                  device_map: str) -> TextPipeline: ...


class VisionSDK(Protocol):
    def process_vision_info(self, messages: list[VisionMessage]) -> tuple[list[Image.Image] | None, list[Tensor] | None]: ...


class TransformsSDK(Protocol):
    def Lambda(self, transform: Callable[[Image.Image], Image.Image]) -> Callable[[Image.Image], Image.Image]: ...
    def ToTensor(self) -> Callable[[Image.Image], Tensor]: ...
    def Compose(self, transforms: Sequence[Callable[[Image.Image], Image.Image] | Callable[[Image.Image], Tensor]]) -> Callable[[Image.Image], Tensor]: ...


class HFResult(TypedDict):
    message: JSONObject
    metadata: object


def completed_results(results: list[HFResult | None]) -> list[HFResult]:
    if any(result is None for result in results):
        raise RuntimeError("a local model request failed before producing a response")
    return [result for result in results if result is not None]


torch = cast(TorchSDK, import_module("torch"))
transformers = cast(TransformersSDK, import_module("transformers"))


class RemoteTransformersSDK[ModelT](Protocol):
    AutoModel: PretrainedLoader[ModelT]
