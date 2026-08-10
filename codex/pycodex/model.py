"""CodexModel — a TaskSolver-contract backend that drives the (instrumented) codex CLI.

The codex subclass of :class:`tasksolver.cli_backend.CLIBackendModel`, sibling of
``pyagy.AgyModel`` / ``tasksolver.claude_code.ClaudeCodeModel`': shells out to ``codex exec`` in a
git workspace and returns the decoded turn. Unlike AgyModel, codex needs real auth — set
``OPENAI_API_KEY`` or ``codex login`` first (``api_key`` is passed through to the run env for the
API-key path).

    from tasksolver.common import TaskSpec, Question
    from pycodex import CodexModel
    model = CodexModel(api_key=None, task=my_task, model="gpt-5-codex")
    parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))
"""
from tasksolver.cli_backend import CLIBackendModel
from tasksolver.common import TaskSpec

from .client import ask_many as _codex_ask_many


class CodexModel(CLIBackendModel):
    backend_label = "codex"
    command_label = "codex exec"
    generic_model_aliases = ("codex",)
    no_output_hint = ("Ensure codex is authenticated (OPENAI_API_KEY or `codex login`) and the "
                      "built binary exists (`pixi install`).")
    # codex has no `Read` tool — it reads files through its own shell/apply_patch sandbox — so it
    # keeps its own wording rather than the base's "Use the Read tool". A prompt-correctness
    # difference, not cosmetic: naming a tool the CLI does not have would degrade vision tasks.
    vision_preamble = "The visual inputs are saved as local image files; read them when answering."
    #: NOTE pycodex.ask_many is SEQUENTIAL (codex writes one fixed capture path per workspace), so
    #: many_rough_guesses(num_threads=n) costs n x wall time here, unlike agy's parallel sampling.
    _client_ask_many = staticmethod(_codex_ask_many)

    def __init__(self, api_key: str = None, task: TaskSpec = None, model: str = None,
                 workspace: str = None, timeout: int = 300, mcp_servers: dict = None,
                 codex_home: str = None):
        # api_key = OPENAI_API_KEY for API-key auth (or None → codex login)
        super().__init__(api_key=api_key, task=task, model=model)
        self.workspace = workspace
        self.timeout = timeout
        # Arbitrary {name: {command, args, env}} MCP servers registered for every
        # run of this model (rendered to `-c mcp_servers.<name>=...` flags).
        self.mcp_servers = mcp_servers
        self.codex_home = codex_home          # scope codex's store (auth/sessions) per model

    def _call_kwargs(self, payload: dict) -> dict:
        kw = dict(workspace=payload.get("workspace") or self.workspace,
                  model=self.model, timeout=self.timeout)
        if self.mcp_servers:
            kw["mcp_servers"] = self.mcp_servers
        if self.codex_home:
            kw["codex_home"] = self.codex_home
        if self.api_key:
            kw["extra_env"] = {"OPENAI_API_KEY": self.api_key}
        return kw

    def session(self, **kwargs):
        """A first-class :class:`pycodex.Session` bound to this model — for multi-turn use
        (``.session_id``, ``.history()``, decoded ``.turns``), mirroring
        ``AgyModel.session()``. Inherits the model, workspace and timeout. ``**kwargs``
        override the Session defaults."""
        from .client import Session
        kw = dict(model=self.model, workspace=self.workspace, timeout=self.timeout,
                  mcp_servers=self.mcp_servers, codex_home=self.codex_home)
        if self.api_key:
            kw["extra_env"] = {"OPENAI_API_KEY": self.api_key}
        kw.update(kwargs)
        return Session(**kw)
