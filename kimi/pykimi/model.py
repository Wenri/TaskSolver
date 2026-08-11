"""KimiCodeModel — a TaskSolver-contract backend that drives the (instrumented) kimi-code CLI.

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
from tasksolver.cli_backend import CLIBackendModel
from tasksolver.common import TaskSpec

from .client import ask_many as _kimi_ask_many

#: The Anthropic-compatible coding endpoint the harness drives (kimi-code's env-family default
#: target); override per-model with ``base_url=``/``provider_type=``.
KIMI_CODE_BASE_URL = "https://api.kimi.com/coding"


class KimiCodeModel(CLIBackendModel):
    backend_label = "kimi-code"
    command_label = "kimi -p"
    generic_model_aliases = ("kimi-code",)
    no_output_hint = ("Ensure a model is defined (MOONSHOT_API_KEY / api_key= for the env-family "
                      "route, or `kimi login`) and the vendored bundle is built (`pixi install`).")
    #: NOTE pykimi.ask_many is SEQUENTIAL (one fixed capture path per workspace), so
    #: many_rough_guesses(num_threads=n) costs n x wall time here, like codex.
    _client_ask_many = staticmethod(_kimi_ask_many)

    def __init__(self, api_key: str = None, task: TaskSpec = None, model: str = None,
                 workspace: str = None, timeout: int = 300, mcp_servers: dict = None,
                 kimi_home: str = None, base_url: str = KIMI_CODE_BASE_URL,
                 provider_type: str = "anthropic", yolo: bool = True):
        # api_key feeds KIMI_MODEL_API_KEY (the env-family model definition); None relies on the
        # CLI's own login/config in $KIMI_CODE_HOME.
        super().__init__(api_key=api_key, task=task, model=model)
        self.workspace = workspace
        self.timeout = timeout
        # Arbitrary {name: {command, args, env}} MCP servers registered for every run of this
        # model (written to the workspace-local mcp.json the CLI discovers).
        self.mcp_servers = mcp_servers
        self.kimi_home = kimi_home            # scope the CLI's store (config/sessions) per model
        self.base_url = base_url
        self.provider_type = provider_type
        self.yolo = yolo                      # print mode cannot prompt: auto-approve tool calls

    def _call_kwargs(self, payload: dict) -> dict:
        kw = dict(workspace=payload.get("workspace") or self.workspace,
                  model=self.model, timeout=self.timeout)
        if self.mcp_servers:
            kw["mcp_servers"] = self.mcp_servers
        if self.kimi_home:
            kw["kimi_home"] = self.kimi_home
        if self.yolo:
            kw["extra_flags"] = ["--yolo"]
        if self.api_key:
            # The env-family model definition — the same contract the harness drives; the CLI
            # builds an ad-hoc model from these without touching config.toml.
            kw["extra_env"] = {
                "KIMI_MODEL_API_KEY": self.api_key,
                "KIMI_MODEL_BASE_URL": self.base_url,
                "KIMI_MODEL_PROVIDER_TYPE": self.provider_type,
            }
        return kw
