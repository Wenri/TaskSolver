"""CodexModel — a TaskSolver-contract backend that drives the (instrumented) codex CLI.

The codex sibling of ``pyagy.AgyModel`` / ``tasksolver.claude_code.ClaudeCodeModel``: shells out
to ``codex exec`` in a git workspace and returns the decoded turn. Unlike AgyModel, codex needs
real auth — set ``OPENAI_API_KEY`` or ``codex login`` first (``api_key`` is passed through to the
run env for the API-key path).

    from tasksolver.common import TaskSpec, Question
    from pycodex import CodexModel
    model = CodexModel(api_key=None, task=my_task, model="gpt-5-codex")
    parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))
"""
from typing import List, Tuple

from loguru import logger

from tasksolver.common import ParsedAnswer, Question, TaskSpec, attach_response_metadata
from tasksolver.exceptions import GPTMaxTriesExceededException, GPTOutputParseException

from .client import ask_many as _codex_ask_many


class CodexModel(object):
    def __init__(self, api_key: str = None, task: TaskSpec = None, model: str = None,
                 workspace: str = None, timeout: int = 300):
        self.api_key = api_key                       # OPENAI_API_KEY for API-key auth (or None → codex login)
        self.task: TaskSpec = task
        # normalize the generic alias to "let codex pick its default"
        self.model: str = model if model not in (None, "codex") else None
        self.workspace = workspace
        self.timeout = timeout

    def _call_kwargs(self, payload: dict) -> dict:
        kw = dict(workspace=payload.get("workspace") or self.workspace,
                  model=self.model, timeout=self.timeout)
        if self.api_key:
            kw["extra_env"] = {"OPENAI_API_KEY": self.api_key}
        return kw

    def _finish(self, r) -> dict:
        if not r.text:
            raise RuntimeError(
                "codex exec returned no output "
                f"(exit_status={r.exit_status}, workspace={r.workspace}). "
                "Ensure codex is authenticated (OPENAI_API_KEY or `codex login`) and the "
                f"built binary exists (`pixi run build-codex`).\nTranscript head:\n{r.transcript[:500]}")
        return {"result": r.text, "transcript": r.transcript, "exit_status": r.exit_status,
                "workspace": r.workspace, "model": r.model, "usage": r.usage}

    def ask(self, payload: dict, n_choices: int = 1) -> Tuple[List[dict], List[dict]]:
        assert n_choices >= 1
        responses = _codex_ask_many(payload["prompt"], n_choices, **self._call_kwargs(payload))
        results = [self._finish(r) for r in responses]
        messages = [{"role": "assistant", "content": res["result"]} for res in results]
        return messages, results

    @staticmethod
    def prepare_payload(question: Question, max_tokens=1000, verbose: bool = False,
                        prepend=None, workspace: str = None, **kwargs) -> dict:
        strings, image_paths = [], []
        for dic in question.get_json(save_local=True):
            if dic["type"] == "text":
                strings.append(dic["text"])
            elif dic["type"] == "image_url":
                local_path = dic.get("local_path")
                if local_path is None:
                    image = dic.get("image")
                    if image is None:
                        raise ValueError("CodexModel needs local image files for vision inputs.")
                    local_path = Question.get_pil_image_content_savecopy(image)["local_path"]
                image_paths.append(local_path)
        parts = []
        if image_paths:
            parts.append("The visual inputs are saved as local image files; read them when answering.")
            parts.extend(f"Image {i}: {p}" for i, p in enumerate(image_paths, 1))
        parts.extend(strings)
        return {"prompt": "\n\n".join(parts), "max_tokens": max_tokens, "workspace": workspace}

    def rough_guess(self, question: Question, max_tokens=1000, max_tries=1,
                    query_id: int = 0, verbose=False, **kwargs):
        p = self.prepare_payload(question, max_tokens=max_tokens, verbose=verbose,
                                 workspace=self.workspace)
        reattempt = 0
        while True:
            response, meta_data = self.ask(p)
            response = response[0]
            try:
                parsed_response = attach_response_metadata(
                    self.task.answer_type.parser(response["content"]),
                    response_metadata=meta_data[0] if isinstance(meta_data, list) and meta_data else meta_data,
                    request_payload=p,
                )
            except GPTOutputParseException:
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException
                logger.warning(f"Reattempt #{reattempt} querying codex")
                continue
            return parsed_response, response, meta_data, p

    def many_rough_guesses(self, num_threads: int, question: Question, max_tokens=1000,
                           verbose=False, max_tries=1) -> List[Tuple[ParsedAnswer, str, dict, dict]]:
        p = self.prepare_payload(question, max_tokens=max_tokens, verbose=verbose,
                                 workspace=self.workspace)
        reattempt = 0
        while True:
            response, meta_data = self.ask(p, n_choices=num_threads)
            try:
                parsed_response = [
                    attach_response_metadata(
                        self.task.answer_type.parser(r["content"]),
                        response_metadata=meta_data[idx] if isinstance(meta_data, list) and len(meta_data) > idx else None,
                        request_payload=p,
                    )
                    for idx, r in enumerate(response)
                ]
            except GPTOutputParseException:
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException
                logger.warning(f"Reattempt #{reattempt} querying codex")
                continue
            return parsed_response, response, meta_data, p

    def run_once(self, question: Question, max_tokens=1000, **kwargs):
        q = self.task.first_question(question)
        return self.rough_guess(q, max_tokens=max_tokens, **kwargs)
