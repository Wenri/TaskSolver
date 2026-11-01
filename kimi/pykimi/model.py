"""KimiCodeModel — a TaskSolver-contract backend that drives the (linked) kimi-code CLI.

The kimi subclass of :class:`tasksolver.cli_backend.CLIBackendModel`, sibling of
``pycodex.CodexModel`` / ``pyagy.AgyModel``: shells out to ``kimi -p`` in a git workspace and
returns the decoded turn. The model rides the CLI's env-var family (``KIMI_MODEL_*``) so no
``kimi login``/config is needed — ``api_key`` (or ``$MOONSHOT_API_KEY`` via the Agent dispatch)
plus the model name fully define the endpoint.

    from tasksolver.common import TaskSpec, Question
    from pykimi import KimiCodeModel
    model = KimiCodeModel(api_key=key, task=my_task, model="k3")
    parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))
"""
from tasksolver.cli_backend import CLIBackendModel, CLIPayload, string_prompt
from tasksolver.common import TaskSpec
from wirecap.runtime.mcp import MCPServers

from .client import AskOptions, KimiResponse, ask_many as _kimi_ask_many

#: The Anthropic-compatible coding endpoint the harness drives (kimi-code's env-family default
#: target); override per-model with ``base_url=``/``provider_type=``.
KIMI_CODE_BASE_URL = "https://api.kimi.com/coding"


class KimiCodeModel(CLIBackendModel[KimiResponse]):
    backend_label = "kimi-code"
    command_label = "kimi -p"
    generic_model_aliases = ("kimi-code",)
    no_output_hint = ("Ensure a model is defined (MOONSHOT_API_KEY / api_key= for the env-family "
                      "route, or `kimi login`) and the vendored bundle is built (`pixi install`).")
    #: NOTE pykimi.ask_many is SEQUENTIAL (one fixed capture path per workspace), so
    #: many_rough_guesses(num_threads=n) costs n x wall time here, like codex.
    _client_ask_many = staticmethod(_kimi_ask_many)

    def __init__(self, api_key: str | None = None, task: TaskSpec | None = None,
                 model: str | None = None, workspace: str | None = None,
                 timeout: float = 300, mcp_servers: MCPServers | None = None,
                 kimi_home: str | None = None, base_url: str = KIMI_CODE_BASE_URL,
                 provider_type: str = "anthropic", yolo: bool = False) -> None:
        # api_key feeds KIMI_MODEL_API_KEY (the env-family model definition); None relies on the
        # CLI's own login/config in $KIMI_CODE_HOME.
        if yolo:
            # `kimi -p --yolo` is rejected by the CLI (`Cannot combine --prompt with --yolo`) and
            # exits 1 with no output. Print mode already auto-approves every tool call, so there
            # is nothing to opt into here; the flag belongs to shell-mode sessions.
            raise ValueError(
                "yolo=True is not usable with this backend: kimi-code rejects --yolo in print "
                "mode, and print mode already auto-approves every tool call.")
        super().__init__(api_key=api_key, task=task, model=model)
        self.workspace = workspace
        self.timeout = timeout
        # Arbitrary {name: {command, args, env}} MCP servers registered for every run of this
        # model (written to the workspace-local mcp.json the CLI discovers).
        self.mcp_servers = mcp_servers
        self.kimi_home = kimi_home            # scope the CLI's store (config/sessions) per model
        self.base_url = base_url
        self.provider_type = provider_type
        self.yolo = False                     # retained for signature compat; see __init__

    def _sample(self, payload: CLIPayload, n_choices: int) -> list[KimiResponse]:
        return self._client_ask_many(string_prompt(payload), n_choices, **self._call_kwargs(payload))

    def _call_kwargs(self, payload: CLIPayload) -> AskOptions:
        kw: AskOptions = {"workspace": payload.get("workspace") or self.workspace,
                          "model": self.model, "timeout": self.timeout}
        if self.mcp_servers:
            kw["mcp_servers"] = self.mcp_servers
        if self.kimi_home:
            kw["kimi_home"] = self.kimi_home
        if self.api_key:
            # The env-family model definition — the same contract the harness drives; the CLI
            # builds an ad-hoc model from these without touching config.toml.
            kw["extra_env"] = {
                "KIMI_MODEL_API_KEY": self.api_key,
                "KIMI_MODEL_BASE_URL": self.base_url,
                "KIMI_MODEL_PROVIDER_TYPE": self.provider_type,
            }
        return kw
