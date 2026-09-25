"""CodexModel — a TaskSolver-contract backend that drives the (instrumented) codex CLI.

The codex subclass of :class:`tasksolver.cli_backend.CLIBackendModel`, sibling of
``pyagy.AgyModel`` / ``tasksolver.claude_code.ClaudeCodeModel``: the official Python SDK drives
the instrumented app-server in a workspace. ``transport="exec"`` retains the legacy exec
transport. Unlike AgyModel, codex needs real auth — set
``OPENAI_API_KEY`` or ``codex login`` first (``api_key`` is passed through to the run env for the
API-key path).

    from tasksolver.common import TaskSpec, Question
    from pycodex import CodexModel
    model = CodexModel(api_key=None, task=my_task, model="gpt-5-codex")
    parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))
"""
import os
from typing import Any, Final

from tasksolver.cli_backend import CLIBackendModel
from tasksolver.common import TaskSpec

from .client import ask_many as _codex_ask_many


class CodexModel(CLIBackendModel):
    backend_label = "codex"
    command_label = "codex exec"
    generic_model_aliases = ("codex", "codex-sdk")
    no_output_hint = ("Ensure codex is authenticated (OPENAI_API_KEY or `codex login`) and the "
                      "built binary exists (`pixi install`).")
    # codex has no `Read` tool — it reads files through its own shell/apply_patch sandbox — so it
    # keeps its own wording rather than the base's "Use the Read tool". A prompt-correctness
    # difference, not cosmetic: naming a tool the CLI does not have would degrade vision tasks.
    vision_preamble = "The visual inputs are saved as local image files; read them when answering."
    #: NOTE pycodex.ask_many is SEQUENTIAL (codex writes one fixed capture path per workspace), so
    #: many_rough_guesses(num_threads=n) costs n x wall time here, unlike agy's parallel sampling.
    _client_ask_many = staticmethod(_codex_ask_many)

    def __init__(self, api_key: str | None = None, task: TaskSpec | None = None,
                 model: str | None = None, workspace: str | None = None, timeout: int = 300,
                 mcp_servers: dict[str, Any] | None = None,
                 codex_home: str | None = None, transport: str = "sdk",
                 sdk_options: dict[str, Any] | None = None):
        # api_key = OPENAI_API_KEY for API-key auth (or None → codex login)
        super().__init__(api_key=api_key, task=task, model=model)
        self.workspace = workspace
        self.timeout = timeout
        # Arbitrary {name: {command, args, env}} MCP servers registered for every
        # run of this model (rendered to `-c mcp_servers.<name>=...` flags).
        self.mcp_servers = mcp_servers
        self.codex_home = codex_home          # scope codex's store (auth/sessions) per model
        if transport not in ("exec", "sdk"):
            raise ValueError("transport must be 'exec' or 'sdk'")
        self.transport = transport
        self.sdk_options = dict(sdk_options or {})
        if transport == "sdk":
            from .sdk import ask_many_sdk
            self._client_ask_many = ask_many_sdk
            self.command_label = "codex app-server"

    def ask(self, payload: dict, n_choices: int = 1):
        if self.transport == "sdk" and payload.get("image_paths"):
            from openai_codex import LocalImageInput, TextInput
            inputs: Final[list[TextInput | LocalImageInput]] = [TextInput(text=payload["prompt"])]
            inputs.extend(LocalImageInput(path=os.path.abspath(path))
                          for path in payload["image_paths"])
            return super().ask({**payload, "prompt": inputs}, n_choices=n_choices)
        return super().ask(payload, n_choices=n_choices)

    def _finish(self, r):
        if self.transport == "sdk" and r.timed_out:
            raise TimeoutError(f"Codex SDK timed out after {self.timeout}s")
        result: Final = super()._finish(r)
        if self.transport == "sdk":
            result.update(session_id=r.session_id, capture_path=r.capture_path,
                          timed_out=r.timed_out, turns=r.turns,
                          sdk_result=r.sdk_result,
                          explicit_reasoning_output=r.explicit_reasoning_output)
        return result

    def _call_kwargs(self, payload: dict) -> dict:
        kw: Final[dict[str, Any]] = dict(workspace=payload.get("workspace") or self.workspace,
                                         model=self.model, timeout=self.timeout)
        if self.mcp_servers:
            kw["mcp_servers"] = self.mcp_servers
        if self.codex_home:
            kw["codex_home"] = self.codex_home
        if self.api_key:
            kw["extra_env"] = {"OPENAI_API_KEY": self.api_key}
        if self.transport == "sdk":
            kw.update(self.sdk_options)
        return kw

    def session(self, **kwargs):
        """A persistent SDK session, or legacy PTY session with ``transport='exec'``.
        Inherits model, workspace, timeout, MCP and auth; kwargs override defaults."""
        if self.transport == "sdk":
            return self.sdk_session(**kwargs)
        from .client import Session
        kw: Final[dict[str, Any]] = dict(model=self.model, workspace=self.workspace,
                                         timeout=self.timeout, mcp_servers=self.mcp_servers,
                                         codex_home=self.codex_home)
        if self.api_key:
            kw["extra_env"] = {"OPENAI_API_KEY": self.api_key}
        kw.update(kwargs)
        return Session(**kw)

    def sdk_session(self, **kwargs):
        """A persistent official-SDK thread using this model's capture/auth settings."""
        from .sdk import SDKSession
        kw: Final[dict[str, Any]] = dict(model=self.model, workspace=self.workspace,
                                         timeout=self.timeout, mcp_servers=self.mcp_servers,
                                         codex_home=self.codex_home)
        if self.api_key:
            kw["extra_env"] = {"OPENAI_API_KEY": self.api_key}
        kw.update(self.sdk_options)
        kw.pop("turn_options", None)
        kw.update(kwargs)
        return SDKSession(**kw)
