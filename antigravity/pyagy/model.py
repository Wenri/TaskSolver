"""AgyModel — a TaskSolver-contract backend that drives the Antigravity `agy` CLI.

The agy subclass of :class:`tasksolver.cli_backend.CLIBackendModel`, shared with
``pycodex.CodexModel`` and ``tasksolver.claude_code.ClaudeCodeModel`` (agy is the same shape: a
local, logged-in agent CLI we shell out to). Uses `agy --print` under a PTY in a git workspace.
No API key needed (agy is logged in via ~/.gemini/antigravity-cli/).

    from tasksolver.common import TaskSpec, Question
    from pyagy import AgyModel
    model = AgyModel(api_key=None, task=my_task, model="gemini-3-pro")
    parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))

``chat=True`` runs each call as the workspace agent :data:`CHAT_AGENT`: its only tools are
``view_file`` (to open the image files, which agy cannot take inline) and the harness's
``manage_task``; it gets none of agy's default prompt sections, skills, rules, plugins, subagents
or MCP servers, reasoning ``--effort low`` unless ``effort`` says otherwise, and the workspace is
not added to agy's global ``trustedWorkspaces``. A turn that calls any other tool, or opens a file
it was not given, raises ``ChatModeViolation``. Needs agy >= 1.2.11 (earlier releases ignore
workspace agents in ``--print`` runs).
"""
import os
from typing import Final

from tasksolver.cli_backend import CLIBackendModel
from tasksolver.common import TaskSpec

from .client import ask_many as _agy_ask_many

#: the workspace agent chat mode runs as (``<workspace>/.agents/agents/<name>/agent.md``)
CHAT_AGENT: Final = "tasksolver-chat"
#: chat mode's default system prompt (the agent definition's body)
CHAT_PROMPT: Final = ("Answer the user's request directly. When the request lists image files, open "
                      "each one with the view_file tool before answering; call no other tool.")
#: ``excludeDefaultComponents`` drops agy's default prompt sections and built-in tools,
#: ``inheritCustomizations: false`` the user's skills, rules, plugins, subagents and MCP servers.
#: Checked with agy 1.2.11: the model is offered view_file and manage_task only, and the
#: prompt shrinks from ~8.7k to ~1.2k tokens.
CHAT_AGENT_MD: Final = """---
name: {name}
description: Answers questions about local image files; opens each listed image with view_file and uses no other tool.
tools:
  - view_file
excludeDefaultComponents: true
inheritCustomizations: false
mainAgent: true
subagent: false
model: inherit
commandExecutionPolicy: off
---
# {name}

{prompt}
"""
#: the only tool a chat turn may call
CHAT_TOOLS: Final = ("view_file",)
#: agy skips its keyring login when these are set (as they are in some tool shells); an agy
#: child always runs on this machine, so chat mode drops them (``None`` = unset)
CHAT_ENV: Final = {"SSH_CLIENT": None, "SSH_CONNECTION": None, "SSH_TTY": None}


class AgyModel(CLIBackendModel):
    backend_label = "agy"
    command_label = "agy --print"
    generic_model_aliases = ("agy",)
    no_output_hint = "Ensure agy is logged in (~/.gemini/antigravity-cli/) and reachable."
    # agy's file tool is view_file (it has no Read tool): naming the wrong tool sent agents
    # searching for the files with run_command
    vision_preamble = ("The visual inputs are saved as local image files. Open each one with the "
                       "view_file tool when answering.")
    # AgyProcess is already a multiprocessing.Process (start() forks agy and returns), so parallel
    # sampling needs no threads: ask_many start()s all n and services them in one event loop
    # (n_choices == 1 is the plain one-shot).
    _client_ask_many = staticmethod(_agy_ask_many)

    def __init__(self, api_key: str = None, task: TaskSpec = None, model: str = None,
                 workspace: str = None, skip_permissions: bool = False,
                 timeout: int = 300, conversation_id: str = None,
                 continue_latest: bool = False, multi_turn: bool = False,
                 data_dir: str = None, print_timeout: int = None,
                 mcp_servers: dict = None, chat: bool = False, effort: str = None,
                 system_prompt: str = None, extra_flags: list = None,
                 extra_env: dict = None):
        # api_key is unused (agy is logged in), kept for contract parity
        super().__init__(api_key=api_key, task=task, model=model)
        if chat:
            if mcp_servers:
                raise ValueError("chat mode runs without MCP servers")
            if skip_permissions:
                raise ValueError("chat mode keeps agy's permission checks")
            from wirecap.runtime.workspace import ensure_git_workspace
            workspace = ensure_git_workspace(workspace)
            write_chat_agent(workspace, system_prompt or CHAT_PROMPT)
        self.chat = chat
        # agy's --effort (low|medium|high); chat mode defaults to low
        self.effort = effort
        self.extra_flags = list(extra_flags or [])
        self.extra_env = dict(extra_env or {})
        self._chat_paths = set()
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
        flags: Final = list(self.extra_flags)
        env: Final = dict(self.extra_env)
        effort: Final = self.effort or ("low" if self.chat else None)
        if self.chat:
            flags += ["--agent", CHAT_AGENT]
            env = {**CHAT_ENV, **env}
        if effort:
            flags += ["--effort", effort]
        kw: Final = dict(
            workspace=payload.get("workspace") or self.workspace,
            model=self.model,
            timeout=self.timeout,
            skip_permissions=self.skip_permissions,
            conversation_id=self.conversation_id,
            continue_latest=(self.continue_latest and self.conversation_id is None),
            data_dir=self.data_dir,
            mcp_servers=self.mcp_servers,
        )
        if flags:
            kw["extra_flags"] = flags
        if env:
            kw["extra_env"] = env
        if self.chat:
            kw["trust"] = False      # no entry in the user's global trustedWorkspaces
        return kw

    def _inline_images(self) -> bool:
        return False                 # agy reads images from files, in chat mode too

    def ask(self, payload: dict, n_choices: int = 1):
        self._chat_paths = {os.path.realpath(p) for p in payload.get("image_paths") or []}
        return super().ask(payload, n_choices=n_choices)

    def _chat_violations(self, r) -> list:
        """Calls other than view_file, and view_file on a file this call did not hand over."""
        used = []
        for turn in r.turns:
            for event in turn.get("events") or []:
                for cand in (event.get("response", event).get("candidates") or []):
                    for part in (cand.get("content") or {}).get("parts") or []:
                        call = part.get("functionCall")
                        if not call:
                            continue
                        name = call.get("name")
                        path = (call.get("args") or {}).get("AbsolutePath")
                        if name not in CHAT_TOOLS:
                            used.append(name)
                        elif not path or os.path.realpath(path) not in self._chat_paths:
                            used.append(f"{name}({path})")
        return used

    def _finish(self, r) -> dict:
        """The base result dict (incl. model/usage, which AgyResponse has always exposed) plus
        agy's conversation id, latched from the first turn so later multi_turn calls resume it
        (--conversation=<id>). n_choices>1 is parallel sampling (no single conversation) — the
        first response's id is taken. Single-threaded (ask() spawns no worker threads; AgyProcess
        is already the native multiprocessing model), so the latch needs no lock."""
        res = super()._finish(r)
        # the answering model's reasoning (the session-title call thinks too; leave it out)
        res["explicit_reasoning_output"] = "\n\n".join(
            t["reasoning"] for t in r.turns if t.get("reasoning") and t.get("model") == r.model)
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


def write_chat_agent(workspace: str, prompt: str = CHAT_PROMPT, name: str = CHAT_AGENT) -> str:
    """Write the chat-mode agent definition into ``workspace``; returns its path."""
    path: Final = os.path.join(workspace, ".agents", "agents", name, "agent.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(CHAT_AGENT_MD.format(name=name, prompt=prompt))
    return path
