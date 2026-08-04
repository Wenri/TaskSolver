"""CLIBackendModel — the shared base for TaskSolver's CLI-subprocess backends.

Three adapters drive a local agent CLI as a TaskSolver backend: `tasksolver.claude_code.
ClaudeCodeModel` (the `claude` binary), `pyagy.AgyModel` (`agy --print` under a PTY) and
`pycodex.CodexModel` (`codex exec`). Everything except `ask` — payload assembly, the
retry-on-parse-failure loop, `run_once`, and the exception context — was independently
triplicated; it lives here now. `ask` is the ONE genuinely per-CLI method (agy and codex fan out
through their wirecap client's `ask_many`; claude-code threads `claude -p` subprocesses), so the
base ships a default the two wirecap backends inherit and ClaudeCodeModel overrides wholesale.

Subclass contract — set the class attributes, then EITHER set `_client_ask_many` (+ `_call_kwargs`
and `no_output_hint`) OR override `ask`:

    class MyModel(CLIBackendModel):
        backend_label = "mycli"                      # retry log: "Reattempt #1 querying mycli"
        command_label = "mycli run"                  # empty-output error subject
        generic_model_aliases = ("mycli",)           # model ids meaning "let the CLI pick"
        vision_preamble = "..."                      # how this CLI is told to read image files
        no_output_hint = "Ensure mycli is logged in."
        _client_ask_many = staticmethod(mycli.ask_many)

The HTTP/SDK adapters (gpt4v, claude, gemini, vllm, kimi, and the local HF ones) are deliberately
NOT in this hierarchy — they share no subprocess/workspace machinery and stay duck-typed.

This module pulls `tasksolver.common` and loguru, so — exactly like the `model.py` files that use
it — it must only ever be imported from a provider's `model.py`, never from a package `__init__`, a
`client.py`, or a `*_process` shim module (those are loaded by the CLI's embedded interpreter,
which cannot import tasksolver; four `python3 -S` probes enforce it).
"""
from typing import List, Tuple

from loguru import logger

from .common import ParsedAnswer, Question, TaskSpec, attach_response_metadata
from .exceptions import GPTMaxTriesExceededException, GPTOutputParseException


class CLIBackendModel(object):
    # --- per-backend knobs (class attributes) ---------------------------------
    backend_label: str = "LLM"          #: the retry log's "querying <label>"
    command_label: str = None           #: empty-output error subject (default: backend_label)
    generic_model_aliases: tuple = ()   #: model ids normalized to None = "let the CLI pick"
    no_output_hint: str = ""            #: how to fix an empty reply (auth / install)
    vision_preamble: str = ("The visual inputs are saved as local image files. "
                            "Use the Read tool to inspect them when answering.")
    #: the provider client's ``ask_many(prompt, n, **kwargs)``; required unless ``ask`` is overridden
    _client_ask_many = None
    #: adapters that shell into a git workspace set this per instance; the rest keep the None default
    workspace = None

    def __init__(self, api_key: str = None, task: TaskSpec = None, model: str = None):
        self.api_key = api_key
        self.task: TaskSpec = task
        # normalize the generic alias to "let the CLI pick its default"
        self.model: str = model if model not in (None, *self.generic_model_aliases) else None

    # --- payload --------------------------------------------------------------
    @classmethod
    def prepare_payload(cls, question: Question, max_tokens=1000, verbose: bool = False,
                        prepend=None, workspace: str = None, **kwargs) -> dict:
        """Flatten a Question into ``{prompt, max_tokens, workspace}``. Image elements are saved to
        local files and announced with ``cls.vision_preamble`` (a CLI reads them off disk).

        A CLASSMETHOD, not a staticmethod, so the preamble and the error message follow the
        subclass — still callable both as ``Model.prepare_payload(q)`` and ``self.prepare_payload(q)``.
        """
        strings, image_paths = [], []
        for dic in question.get_json(save_local=True):
            if dic["type"] == "text":
                strings.append(dic["text"])
            elif dic["type"] == "image_url":
                local_path = dic.get("local_path")
                if local_path is None:
                    image = dic.get("image")
                    if image is None:
                        raise ValueError(f"{cls.__name__} needs local image files for vision inputs.")
                    local_path = Question.get_pil_image_content_savecopy(image)["local_path"]
                image_paths.append(local_path)

        parts = []
        if image_paths:
            parts.append(cls.vision_preamble)
            parts.extend(f"Image {i}: {p}" for i, p in enumerate(image_paths, 1))
        parts.extend(strings)
        return {"prompt": "\n\n".join(parts), "max_tokens": max_tokens, "workspace": workspace}

    # --- the provider call ----------------------------------------------------
    def _call_kwargs(self, payload: dict) -> dict:
        """Kwargs for ``_client_ask_many``, assembled from this model's config."""
        return {}

    def _check_output(self, r) -> None:
        """Raise when the CLI produced no answer at all (auth / install failures land here)."""
        if not r.text:
            raise RuntimeError(
                f"{self.command_label or self.backend_label} returned no output "
                f"(exit_status={r.exit_status}, workspace={r.workspace}). "
                f"{self.no_output_hint}\nTranscript head:\n{r.transcript[:500]}")

    def _finish(self, r) -> dict:
        """Validate one client response object and shape the per-choice result dict."""
        self._check_output(r)
        return {"result": r.text, "transcript": r.transcript, "exit_status": r.exit_status,
                "workspace": r.workspace, "model": r.model, "usage": r.usage}

    def ask(self, payload: dict, n_choices: int = 1) -> Tuple[List[dict], List[dict]]:
        """Run ``n_choices`` samples through the provider client → ``(messages, metadata)``.
        Override ENTIRELY when the CLI is not driven by a wirecap ``ask_many`` (ClaudeCodeModel
        threads its own subprocesses and returns the raw CLI JSON as metadata)."""
        assert n_choices >= 1
        if self._client_ask_many is None:
            raise NotImplementedError(
                f"{type(self).__name__} must set _client_ask_many or override ask()")
        responses = self._client_ask_many(payload["prompt"], n_choices, **self._call_kwargs(payload))
        results = [self._finish(r) for r in responses]
        messages = [{"role": "assistant", "content": res["result"]} for res in results]
        return messages, results

    # --- retry-on-parse-failure (the universal loop) --------------------------
    def _max_tries_exceeded(self, max_tries, reattempt, exc, raw_response, response_metadata,
                            request_payload):
        """The ONE place these adapters build GPTMaxTriesExceededException — with the failed
        attempt's context attached, per CLAUDE.md (all six raise sites used to pass none)."""
        logger.error(f"max tries ({max_tries}) exceeded.")
        return GPTMaxTriesExceededException(
            f"Failed to parse {self.backend_label} response after {reattempt} attempts: {exc}",
            raw_response=raw_response,
            response_metadata=response_metadata,
            request_payload=request_payload,
        )

    def rough_guess(self, question: Question, max_tokens=1000, max_tries=1,
                    query_id: int = 0, verbose=False, **kwargs):
        p = self.prepare_payload(question, max_tokens=max_tokens, verbose=verbose,
                                 prepend=None, workspace=self.workspace)
        reattempt = 0
        while True:
            response, meta_data = self.ask(p)
            response = response[0]
            try:
                parsed_response = attach_response_metadata(
                    self.task.answer_type.parser(response["content"]),
                    response_metadata=(meta_data[0] if isinstance(meta_data, list) and meta_data
                                       else meta_data),
                    request_payload=p,
                )
            except GPTOutputParseException as exc:
                reattempt += 1
                if reattempt > max_tries:
                    raise self._max_tries_exceeded(
                        max_tries, reattempt, exc, response, meta_data, p) from exc
                logger.warning(f"Reattempt #{reattempt} querying {self.backend_label}")
                continue
            return parsed_response, response, meta_data, p

    def many_rough_guesses(self, num_threads: int, question: Question, max_tokens=1000,
                           verbose=False, max_tries=1) -> List[Tuple[ParsedAnswer, str, dict, dict]]:
        p = self.prepare_payload(question, max_tokens=max_tokens, verbose=verbose,
                                 prepend=None, workspace=self.workspace)
        reattempt = 0
        while True:
            response, meta_data = self.ask(p, n_choices=num_threads)
            try:
                parsed_response = [
                    attach_response_metadata(
                        self.task.answer_type.parser(r["content"]),
                        response_metadata=(meta_data[idx]
                                           if isinstance(meta_data, list) and len(meta_data) > idx
                                           else None),
                        request_payload=p,
                    )
                    for idx, r in enumerate(response)
                ]
            except GPTOutputParseException as exc:
                reattempt += 1
                if reattempt > max_tries:
                    raise self._max_tries_exceeded(
                        max_tries, reattempt, exc, response, meta_data, p) from exc
                logger.warning(f"Reattempt #{reattempt} querying {self.backend_label}")
                continue
            return parsed_response, response, meta_data, p

    def run_once(self, question: Question, max_tokens=1000, **kwargs):
        q = self.task.first_question(question)
        return self.rough_guess(q, max_tokens=max_tokens, **kwargs)
