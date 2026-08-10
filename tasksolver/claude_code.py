"""Claude Code CLI adapter for local Pro-subscription evaluation."""

import json
import os
import subprocess
import threading
from glob import glob
from typing import List, Tuple

from .cli_backend import CLIBackendModel
from .common import TaskSpec


class ClaudeCodeModel(CLIBackendModel):
    backend_label = "LLM"                        # preserves "Reattempt #N querying LLM"
    command_label = "claude -p"
    generic_model_aliases = ("claude-code",)
    no_output_hint = "Ensure the claude CLI is installed and logged in (`claude /login`)."
    # vision_preamble: the base default IS claude-code's wording (it has a Read tool).
    # No _client_ask_many / _call_kwargs / _finish — `ask` below is overridden wholesale: this CLI
    # is driven by threaded `claude -p` subprocesses, not a wirecap ask_many, and its metadata is
    # the raw CLI JSON rather than a _finish-shaped dict.

    def __init__(self, api_key: str, task: TaskSpec, model: str = None, *,
                 mcp_servers: dict = None, workspace: str = None,
                 allowed_tools: str = "Read", permission_mode: str = "acceptEdits"):
        # api_key/task stay REQUIRED here (the base defaults both to None); the generic-alias
        # normalization comes from generic_model_aliases above.
        super().__init__(api_key=api_key, task=task, model=model)
        self.claude_key: str = api_key          # legacy attribute name, kept
        self.thinking_depth = None
        # Arbitrary {name: {command, args, env}} MCP servers, loaded per call via
        # --mcp-config + --strict-mcp-config (the config file is written once per
        # instance). With MCP servers the default `--tools Read` restriction would
        # hide them — pass allowed_tools=None to lift the restriction, or an
        # explicit list matching your CLI's tool-name syntax.
        self.mcp_servers = mcp_servers
        self.workspace = workspace              # cwd for the CLI (None = inherit)
        self.allowed_tools = allowed_tools
        self.permission_mode = permission_mode
        self._mcp_config_path = None            # written lazily on first use

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
        if self.thinking_depth:
            cmd.extend(["--effort", self.thinking_depth])
            legacy_cmd.extend(["--effort", self.thinking_depth])

        try:
            completed = self._run_cli_command(cmd, cwd=self.workspace)
            if completed.returncode != 0 and "unknown option" in completed.stderr.lower():
                completed = self._run_cli_command(legacy_cmd, cwd=self.workspace)
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
                "Use a family-qualified alias such as `claude-code-sonnet-4-6`, "
                "`claude-code-opus-4-7`, or `claude-code-fable-5`.\n"
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

    def _mcp_args(self) -> List[str]:
        """``--mcp-config``/``--strict-mcp-config`` for this instance's servers.
        The config file is written once, into the workspace (or a private temp
        dir when no workspace is set) so parallel instances never collide."""
        if not self.mcp_servers:
            return []
        if self._mcp_config_path is None:
            from wirecap.runtime.mcp import claude_mcp_args
            base = self.workspace
            if not base:
                import tempfile
                base = tempfile.mkdtemp(prefix="claude-code-mcp-")
            path = os.path.join(base, "mcp-servers.json")
            args = claude_mcp_args(self.mcp_servers, path)
            self._mcp_config_path = path
            self._mcp_cli_args = args
        return list(self._mcp_cli_args)

    def _build_cli_command(self, prompt: str, tool_flag: str) -> List[str]:
        cmd = [
            self._claude_command(),
            "-p",
            prompt,
            "--output-format",
            "json",
        ]
        if self.allowed_tools is not None:
            cmd += [tool_flag, self.allowed_tools]
        cmd += ["--permission-mode", self.permission_mode]
        cmd += self._mcp_args()
        return cmd

    @staticmethod
    def _claude_command() -> str:
        cask_paths = sorted(glob("/opt/homebrew/Caskroom/claude-code/*/claude"))
        for path in reversed(cask_paths):
            if os.access(path, os.X_OK):
                return path
        return "claude"

    @staticmethod
    def _run_cli_command(cmd: List[str], cwd: str = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
