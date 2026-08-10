"""AgyModel — a TaskSolver-contract backend that drives the Antigravity `agy` CLI.

The agy subclass of :class:`tasksolver.cli_backend.CLIBackendModel`, shared with
``pycodex.CodexModel`` and ``tasksolver.claude_code.ClaudeCodeModel`` (agy is the same shape: a
local, logged-in agent CLI we shell out to). Uses `agy --print` under a PTY in a git workspace.
No API key needed (agy is logged in via ~/.gemini/antigravity-cli/).

    from tasksolver.common import TaskSpec, Question
    from pyagy import AgyModel
    model = AgyModel(api_key=None, task=my_task, model="gemini-3-pro")
    parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))
"""
from tasksolver.cli_backend import CLIBackendModel
from tasksolver.common import TaskSpec

from .client import ask_many as _agy_ask_many


class AgyModel(CLIBackendModel):
    backend_label = "agy"
    command_label = "agy --print"
    generic_model_aliases = ("agy",)
    no_output_hint = "Ensure agy is logged in (~/.gemini/antigravity-cli/) and reachable."
    # agy has a Read tool, so the base's vision preamble is already correct (no override).
    # AgyProcess is already a multiprocessing.Process (start() forks agy and returns), so parallel
    # sampling needs no threads: ask_many start()s all n and services them in one event loop
    # (n_choices == 1 is the plain one-shot).
    _client_ask_many = staticmethod(_agy_ask_many)

    def __init__(self, api_key: str = None, task: TaskSpec = None, model: str = None,
                 workspace: str = None, skip_permissions: bool = False,
                 timeout: int = 300, conversation_id: str = None,
                 continue_latest: bool = False, multi_turn: bool = False,
                 data_dir: str = None, print_timeout: int = None,
                 mcp_servers: dict = None):
        # api_key is unused (agy is logged in), kept for contract parity
        super().__init__(api_key=api_key, task=task, model=model)
        self.workspace = workspace
        self.skip_permissions = skip_permissions
        # Arbitrary {name: {command, args, env}} MCP servers registered for every
        # run of this model (see pyagy.client._inject_config; pair with data_dir
        # to keep them out of the user's global agy config).
        self.mcp_servers = mcp_servers
        # `timeout` matches CodexModel and both clients; print_timeout was the odd name out (it was
        # translated straight back to timeout= for the client). Kept as a deprecated alias so
        # external callers constructing AgyModel(print_timeout=...) keep working.
        self.timeout = print_timeout if print_timeout is not None else timeout
        # Opt-in multi-turn: continue ONE agy conversation across calls (see pyagy.Session).
        # Off by default → each call is an independent one-shot (the classic adapter shape).
        # Enabled implicitly when a conversation is being resumed.
        self.conversation_id = conversation_id
        self.continue_latest = continue_latest
        self.multi_turn = bool(multi_turn or conversation_id or continue_latest)
        self.data_dir = data_dir              # scope the conversation store to a project repo

    def _call_kwargs(self, payload: dict) -> dict:
        """Shared client.ask_many kwargs assembled from this model's config."""
        return dict(
            workspace=payload.get("workspace") or self.workspace,
            model=self.model,
            timeout=self.timeout,
            skip_permissions=self.skip_permissions,
            conversation_id=self.conversation_id,
            continue_latest=(self.continue_latest and self.conversation_id is None),
            data_dir=self.data_dir,
            mcp_servers=self.mcp_servers,
        )

    def _finish(self, r) -> dict:
        """The base result dict (incl. model/usage, which AgyResponse has always exposed) plus
        agy's conversation id, latched from the first turn so later multi_turn calls resume it
        (--conversation=<id>). n_choices>1 is parallel sampling (no single conversation) — the
        first response's id is taken. Single-threaded (ask() spawns no worker threads; AgyProcess
        is already the native multiprocessing model), so the latch needs no lock."""
        res = super()._finish(r)
        if self.multi_turn and self.conversation_id is None:
            self.conversation_id = r.conversation_id
        res["conversation_id"] = self.conversation_id
        return res

    def session(self, **kwargs):
        """A first-class :class:`pyagy.Session` bound to this model — for rich multi-turn
        use (``.conversation_id``, ``.history()``, decoded ``.turns``). Inherits the model,
        workspace, and skip-permissions, and resumes this model's ``conversation_id`` if it
        has latched one. ``**kwargs`` override the Session defaults."""
        from .client import Session
        kw = dict(model=self.model, workspace=self.workspace,
                  skip_permissions=self.skip_permissions, timeout=self.timeout,
                  data_dir=self.data_dir, mcp_servers=self.mcp_servers)
        if self.conversation_id:
            kw["conversation_id"] = self.conversation_id
        elif self.continue_latest:
            kw["continue_latest"] = True
        kw.update(kwargs)
        return Session(**kw)
