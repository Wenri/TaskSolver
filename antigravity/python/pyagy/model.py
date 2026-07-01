"""AgyModel — a TaskSolver-contract backend that drives the Antigravity `agy` CLI.

Mirrors tasksolver.claude_code.ClaudeCodeModel (agy is the same shape: a local,
logged-in agent CLI we shell out to). Uses `agy --print` under a PTY in a git
workspace. No API key needed (agy is logged in via ~/.gemini/antigravity-cli/).

    from tasksolver.common import TaskSpec, Question
    from pyagy import AgyModel
    model = AgyModel(api_key=None, task=my_task, model="gemini-3-pro")
    parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))
"""
import threading
from typing import List, Tuple

from loguru import logger

from tasksolver.common import ParsedAnswer, Question, TaskSpec, attach_response_metadata
from tasksolver.exceptions import GPTMaxTriesExceededException, GPTOutputParseException

from .session import run_print


class AgyModel(object):
    def __init__(self, api_key: str = None, task: TaskSpec = None, model: str = None,
                 workspace: str = None, skip_permissions: bool = False,
                 print_timeout: int = 300):
        self.api_key = api_key                       # unused (agy is logged in), kept for contract parity
        self.task: TaskSpec = task
        # normalize the generic alias to "let agy pick"
        self.model: str = model if model not in (None, "agy") else None
        self.workspace = workspace
        self.skip_permissions = skip_permissions
        self.print_timeout = print_timeout

    def _query_once(self, payload: dict) -> dict:
        r = run_print(
            payload["prompt"],
            workdir=payload.get("workspace") or self.workspace,
            model=self.model,
            timeout=self.print_timeout,
            skip_permissions=self.skip_permissions,
        )
        if not r["result"]:
            raise RuntimeError(
                "agy --print returned no output "
                f"(exit_status={r['exit_status']}, workspace={r['workspace']}). "
                "Ensure agy is logged in (~/.gemini/antigravity-cli/) and reachable. "
                f"Transcript head:\n{r['transcript'][:500]}"
            )
        return r

    def ask(self, payload: dict, n_choices: int = 1) -> Tuple[List[dict], List[dict]]:
        def worker(idx, results):
            try:
                raw = self._query_once(payload)
                results[idx] = {
                    "message": {"role": "assistant", "content": raw["result"]},
                    "metadata": raw,
                }
            except BaseException as e:  # stash to re-raise on the caller thread
                results[idx] = e

        assert n_choices >= 1
        results = [None] * n_choices
        if n_choices > 1:
            jobs = [threading.Thread(target=worker, args=(i, results)) for i in range(n_choices)]
            for j in jobs:
                j.start()
            for j in jobs:
                j.join()
        else:
            worker(0, results)

        # Propagate the first worker error instead of crashing later on a None result.
        for r in results:
            if isinstance(r, BaseException):
                raise r

        messages = [res["message"] for res in results]
        metadata = [res["metadata"] for res in results]
        return messages, metadata

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
                        raise ValueError("AgyModel needs local image files for vision inputs.")
                    local_path = Question.get_pil_image_content_savecopy(image)["local_path"]
                image_paths.append(local_path)

        parts = []
        if image_paths:
            parts.append("The visual inputs are saved as local image files. Use the Read "
                         "tool to inspect them when answering.")
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
                logger.warning(f"Reattempt #{reattempt} querying agy")
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
                logger.warning(f"Reattempt #{reattempt} querying agy")
                continue
            return parsed_response, response, meta_data, p

    def run_once(self, question: Question, max_tokens=1000, **kwargs):
        q = self.task.first_question(question)
        return self.rough_guess(q, max_tokens=max_tokens, **kwargs)
