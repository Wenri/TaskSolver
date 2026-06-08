"""Claude Code CLI adapter for local Pro-subscription evaluation."""

import json
import os
import subprocess
import threading
from glob import glob
from typing import List, Tuple

from loguru import logger

from .common import ParsedAnswer, Question, TaskSpec, attach_response_metadata
from .exceptions import GPTMaxTriesExceededException, GPTOutputParseException


class ClaudeCodeModel(object):
    def __init__(self, api_key: str, task: TaskSpec, model: str = None):
        self.claude_key: str = api_key
        self.task: TaskSpec = task
        self.model: str = model if model not in (None, "claude-code") else None

    def ask(self, payload: dict, n_choices=1) -> Tuple[List[dict], List[dict]]:
        """
        Args:
            payload: json dictionary, prepared by `prepare_payload`
        """

        def claude_code_thread(idx, payload, results):
            raw_response = self._query_once(payload)
            content = raw_response.get("result", raw_response.get("stdout", "")).strip()
            results[idx] = {
                "message": {"role": "assistant", "content": content},
                "metadata": raw_response,
            }

        assert n_choices >= 1

        results = [None] * n_choices
        if n_choices > 1:
            claude_code_jobs = [
                threading.Thread(target=claude_code_thread, args=(idx, payload, results))
                for idx in range(n_choices)
            ]
            for job in claude_code_jobs:
                job.start()
            for job in claude_code_jobs:
                job.join()
        else:
            claude_code_thread(0, payload, results)

        messages: List[dict] = [res["message"] for res in results]
        metadata: List[dict] = [res["metadata"] for res in results]
        return messages, metadata

    def _query_once(self, payload: dict) -> dict:
        cmd = self._build_cli_command(payload["prompt"], tool_flag="--tools")
        legacy_cmd = self._build_cli_command(payload["prompt"], tool_flag="--allowedTools")
        if self.model:
            cmd.extend(["--model", self.model])
            legacy_cmd.extend(["--model", self.model])

        try:
            completed = self._run_cli_command(cmd)
            if completed.returncode != 0 and "unknown option" in completed.stderr.lower():
                completed = self._run_cli_command(legacy_cmd)
        except FileNotFoundError as e:
            raise RuntimeError(
                "Claude Code CLI was not found. Install it with "
                "`npm install -g @anthropic-ai/claude-code`, then run "
                "`claude auth login` and log in with your Claude Pro account."
            ) from e

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if completed.returncode != 0:
            raise RuntimeError(self._format_cli_failure(stdout, stderr))

        try:
            parsed = json.loads(stdout)
            parsed["stdout"] = stdout
            parsed["stderr"] = stderr
            return parsed
        except json.JSONDecodeError:
            return {"result": stdout, "stdout": stdout, "stderr": stderr}

    @staticmethod
    def _format_cli_failure(stdout: str, stderr: str) -> str:
        combined_output = "\n".join(part for part in (stderr, stdout) if part).strip()
        if not combined_output:
            combined_output = "(no CLI output)"

        parsed_output = None
        try:
            parsed_output = json.loads(stdout or stderr)
        except json.JSONDecodeError:
            parsed_output = None

        if isinstance(parsed_output, dict) and parsed_output.get("api_error_status") == 404:
            result = parsed_output.get("result") or "Selected Claude Code model was not found or is not accessible."
            return (
                "Claude Code CLI model is unavailable. "
                "Check the model alias and your Claude account access. "
                "Use a family-qualified alias such as `claude-code-sonnet-4-6` "
                "or `claude-code-opus-4-7`.\n"
                f"CLI result: {result}\n"
                f"CLI output:\n{combined_output}"
            )

        lowered_output = combined_output.lower()
        if "not logged in" in lowered_output or "please run /login" in lowered_output:
            return (
                "Claude Code CLI is not logged in for prompt execution. "
                "Run `claude /login` in the Claude Code CLI, then verify prompt execution with "
                "`claude -p \"Reply with OK\" --output-format json`.\n"
                f"CLI output:\n{combined_output}"
            )

        return f"Claude Code CLI call failed.\nCLI output:\n{combined_output}"

    @staticmethod
    def _build_cli_command(prompt: str, tool_flag: str) -> List[str]:
        return [
            ClaudeCodeModel._claude_command(),
            "-p",
            prompt,
            "--output-format",
            "json",
            tool_flag,
            "Read",
            "--permission-mode",
            "acceptEdits",
        ]

    @staticmethod
    def _claude_command() -> str:
        cask_paths = sorted(glob("/opt/homebrew/Caskroom/claude-code/*/claude"))
        for path in reversed(cask_paths):
            if os.access(path, os.X_OK):
                return path
        return "claude"

    @staticmethod
    def _run_cli_command(cmd: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def prepare_payload(
        question: Question,
        max_tokens=1000,
        verbose: bool = False,
        prepend=None,
        **kwargs,
    ) -> dict:
        strings = []
        image_paths = []
        for dic in question.get_json(save_local=True):
            if dic["type"] == "text":
                strings.append(dic["text"])
            elif dic["type"] == "image_url":
                local_path = dic.get("local_path")
                if local_path is None:
                    image = dic.get("image")
                    if image is None:
                        raise ValueError("ClaudeCodeModel needs local image files for vision inputs.")
                    saved = Question.get_pil_image_content_savecopy(image)
                    local_path = saved["local_path"]
                image_paths.append(local_path)

        prompt_parts = []
        if image_paths:
            prompt_parts.append(
                "The visual inputs are saved as local image files. Use the Read tool "
                "to inspect them when answering."
            )
            prompt_parts.extend(
                [f"Image {idx}: {path}" for idx, path in enumerate(image_paths, start=1)]
            )
        prompt_parts.extend(strings)

        return {
            "prompt": "\n\n".join(prompt_parts),
            "max_tokens": max_tokens,
        }

    def rough_guess(
        self,
        question: Question,
        max_tokens=1000,
        max_tries=1,
        query_id: int = 0,
        verbose=False,
        **kwargs,
    ):
        p = self.prepare_payload(question, max_tokens=max_tokens, verbose=verbose, prepend=None)

        ok = False
        reattempt = 0
        while not ok:
            response, meta_data = self.ask(p)
            response = response[0]
            try:
                parsed_response = attach_response_metadata(
                    self.task.answer_type.parser(response["content"]),
                    response_metadata=meta_data[0] if isinstance(meta_data, list) and len(meta_data) > 0 else meta_data,
                    request_payload=p,
                )
            except GPTOutputParseException:
                reattempt += 1
                if reattempt > max_tries:
                    logger.error(f"max tries ({max_tries}) exceeded.")
                    raise GPTMaxTriesExceededException

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            ok = True

        return parsed_response, response, meta_data, p

    def many_rough_guesses(
        self,
        num_threads: int,
        question: Question,
        max_tokens=1000,
        verbose=False,
        max_tries=1,
    ) -> List[Tuple[ParsedAnswer, str, dict, dict]]:
        p = self.prepare_payload(question, max_tokens=max_tokens, verbose=verbose, prepend=None)

        n_choices = num_threads
        ok = False
        reattempt = 0
        while not ok:
            response, meta_data = self.ask(p, n_choices=n_choices)
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

                logger.warning(f"Reattempt #{reattempt} querying LLM")
                continue
            ok = True

        return parsed_response, response, meta_data, p

    def run_once(self, question: Question, max_tokens=1000, **kwargs):
        q = self.task.first_question(question)
        p_ans, ans, meta, p = self.rough_guess(q, max_tokens=max_tokens, **kwargs)
        return p_ans, ans, meta, p
