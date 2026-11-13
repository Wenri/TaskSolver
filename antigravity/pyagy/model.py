"""AgyModel — a TaskSolver-contract backend that drives the Antigravity `agy` CLI.

The agy subclass of :class:`tasksolver.cli_backend.CLIBackendModel`, shared with
``pycodex.CodexModel`` and ``tasksolver.claude_code.ClaudeCodeModel`` (agy is the same shape: a
local, logged-in agent CLI we shell out to). Uses `agy --print` under a PTY in a git workspace.
No API key needed (agy is logged in via ~/.gemini/antigravity-cli/).

    from tasksolver.common import ResponseMetadata, TaskSpec
from wirecap.runtime.mcp import MCPServers
from wirecap.types import JSONObject, json_array, json_object, Question
    from pyagy import AgyModel
    model = AgyModel(api_key=None, task=my_task, model="gemini-3-pro")
    parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))

``chat=True`` runs each call as the workspace agent :data:`CHAT_AGENT`: its only tools are
``read_file`` (to open the image files, which agy cannot take inline) and the harness's
``manage_task``; it gets none of agy's default prompt sections, skills, rules, plugins, subagents
or MCP servers, reasoning ``--effort low`` unless ``effort`` says otherwise, and the workspace is
not added to agy's global ``trustedWorkspaces``. A turn that calls any other tool, or opens a file
it was not given, raises ``ChatModeViolation``. Written for agy 1.5, whose file reader is
``read_file`` (``view_file`` up to agy 1.2.x; releases before 1.2.11 ignore workspace agents in
``--print`` runs).
"""
import os
from collections.abc import Mapping, Sequence
from typing import Final, Unpack, cast

from tasksolver.cli_backend import CLIBackendModel, CLIPayload, Message, string_prompt
from tasksolver.common import ResponseMetadata, TaskSpec
from wirecap.runtime.mcp import MCPServers
from wirecap.types import JSONObject, json_array, json_object

from .client import AgyResponse, AskOptions, Session, SessionOptions, ask_many as _agy_ask_many

#: the workspace agent chat mode runs as (``<workspace>/.agents/agents/<name>/agent.md``)
CHAT_AGENT: Final = "tasksolver-chat"
#: chat mode's default system prompt (the agent definition's body)
CHAT_PROMPT: Final = ("Answer the user's request directly. When the request lists image files, open "
                      "each one with the read_file tool before answering; call no other tool.")
#: ``excludeDefaultComponents`` drops agy's default prompt sections and built-in tools,
#: ``inheritCustomizations: false`` the user's skills, rules, plugins, subagents and MCP servers.
#: Checked with agy 1.5.5: the model is offered read_file and manage_task only (13 tools by
#: default), and the first request shrinks from ~14.4k to ~0.7k input tokens. Up to agy 1.2.x the
#: reader was view_file (1.2.11: view_file and manage_task, ~8.7k to ~1.2k tokens). agy 1.5.5 still
#: resolves ``view_file`` in ``tools`` to read_file, so a definition that lists both names fails
#: (agy's error: tool name conflict: multiple tools resolved with name "read_file").
CHAT_AGENT_MD: Final = """---
name: {name}
description: Answers questions about local image files; opens each listed image with read_file and uses no other tool.
tools:
  - read_file
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
#: the only tool a chat turn may call: agy's file reader (agy 1.2.x: view_file) ...
CHAT_TOOLS: Final = ("read_file",)
#: ... and the argument that names the file it opens (agy 1.2.x: AbsolutePath)
CHAT_PATH_ARG: Final = "Path"
#: agy skips its keyring login when these are set (as they are in some tool shells); an agy
#: child always runs on this machine, so chat mode drops them (``None`` = unset)
CHAT_ENV: Final = {"SSH_CLIENT": None, "SSH_CONNECTION": None, "SSH_TTY": None}


class AgyModel(CLIBackendModel[AgyResponse]):
    backend_label = "agy"
    command_label = "agy --print"
    generic_model_aliases = ("agy",)
    no_output_hint = "Ensure agy is logged in (~/.gemini/antigravity-cli/) and reachable."
    # agy's file tool is read_file (view_file up to agy 1.2.x; it has no Read tool): naming the
    # wrong tool sent agents searching for the files with run_command
    vision_preamble = ("The visual inputs are saved as local image files. Open each one with the "
                       "read_file tool when answering.")
    # AgyProcess is already a multiprocessing.Process (start() forks agy and returns), so parallel
    # sampling needs no threads: ask_many start()s all n and services them in one event loop
    # (n_choices == 1 is the plain one-shot).
    _client_ask_many = staticmethod(_agy_ask_many)

    def __init__(self, api_key: str | None = None, task: TaskSpec | None = None,
                 model: str | None = None, workspace: str | None = None,
                 skip_permissions: bool = False, timeout: float = 300,
                 conversation_id: str | None = None, continue_latest: bool = False,
                 multi_turn: bool = False, data_dir: str | None = None,
                 app_data_dir: str | None = None,
                 print_timeout: float | None = None, mcp_servers: MCPServers | None = None,
                 chat: bool = False, effort: str | None = None, system_prompt: str | None = None,
                 extra_flags: Sequence[str] | None = None,
                 extra_env: Mapping[str, str | None] | None = None) -> None:
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
        self._chat_paths: set[str] = set()
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
        self.app_data_dir = app_data_dir      # store subdir under ~/.gemini (default: pyagy)

    def _sample(self, payload: CLIPayload, n_choices: int) -> list[AgyResponse]:
        return self._client_ask_many(string_prompt(payload), n_choices, **self._call_kwargs(payload))

    def _call_kwargs(self, payload: CLIPayload) -> AskOptions:
        """Shared client.ask_many kwargs assembled from this model's config."""
        flags = list(self.extra_flags)
        env: dict[str, str | None] = dict(self.extra_env)
        effort: Final = self.effort or ("low" if self.chat else None)
        if self.chat:
            flags += ["--agent", CHAT_AGENT]
            env = {**CHAT_ENV, **env}
        if effort:
            flags += ["--effort", effort]
        kw: AskOptions = {
            "workspace": payload.get("workspace") or self.workspace,
            "model": self.model,
            "timeout": self.timeout,
            "skip_permissions": self.skip_permissions,
            "conversation_id": self.conversation_id,
            "continue_latest": self.continue_latest and self.conversation_id is None,
            "data_dir": self.data_dir,
            "mcp_servers": self.mcp_servers,
        }
        if self.app_data_dir is not None:
            kw["app_data_dir"] = self.app_data_dir
        if flags:
            kw["extra_flags"] = flags
        if env:
            kw["extra_env"] = env
        if self.chat:
            kw["trust"] = False      # no entry in the user's global trustedWorkspaces
        return kw

    def _inline_images(self) -> bool:
        return False                 # agy reads images from files, in chat mode too

    def ask(self, payload: CLIPayload, n_choices: int = 1) -> tuple[list[Message], list[ResponseMetadata]]:
        self._chat_paths = {os.path.realpath(p) for p in payload.get("image_paths") or []}
        return super().ask(payload, n_choices=n_choices)

    def _chat_violations(self, r: AgyResponse) -> list[str]:
        """Calls other than read_file, and read_file on a file this call did not hand over."""
        used: list[str] = []
        for turn in r.turns:
            events = turn.get("events")
            if not isinstance(events, list):
                continue
            for event in cast(list[JSONObject], events):
                response = json_object(event.get("response", event))
                for candidate in json_array(response.get("candidates") or []):
                    content = json_object(json_object(candidate).get("content") or {})
                    for part in json_array(content.get("parts") or []):
                        call = json_object(part).get("functionCall")
                        if not call:
                            continue
                        call = json_object(call)
                        name = call.get("name")
                        path = json_object(call.get("args") or {}).get(CHAT_PATH_ARG)
                        if name not in CHAT_TOOLS:
                            used.append(str(name))
                        elif not isinstance(path, str) or not path or os.path.realpath(path) not in self._chat_paths:
                            used.append(f"{name}({path})")
        return used

    def _finish(self, r: AgyResponse) -> ResponseMetadata:
        """The base result dict (incl. model/usage, which AgyResponse has always exposed) plus
        agy's conversation id, latched from the first turn so later multi_turn calls resume it
        (--conversation=<id>). n_choices>1 is parallel sampling (no single conversation) — the
        first response's id is taken. Single-threaded (ask() spawns no worker threads; AgyProcess
        is already the native multiprocessing model), so the latch needs no lock."""
        res = super()._finish(r)
        # the answering model's reasoning (the session-title call thinks too; leave it out)
        res["explicit_reasoning_output"] = "\n\n".join(
            reasoning for t in r.turns if isinstance(reasoning := t.get("reasoning"), str)
            and reasoning and t.get("model") == r.model)
        if self.multi_turn and self.conversation_id is None:
            self.conversation_id = r.conversation_id
        res["conversation_id"] = self.conversation_id
        res["system_messages"] = r.system_messages
        res["system_message_errors"] = r.system_message_errors
        res["response_capture_errors"] = r.response_capture_errors
        return res

    def session(self, **kwargs: Unpack[SessionOptions]) -> Session:
        """A first-class :class:`pyagy.Session` bound to this model — for rich multi-turn
        use (``.conversation_id``, ``.history()``, decoded ``.turns``). Inherits the model,
        workspace, and skip-permissions, and resumes this model's ``conversation_id`` if it
        has latched one. ``**kwargs`` override the Session defaults."""
        kw: SessionOptions = {"model": self.model, "workspace": self.workspace,
                  "skip_permissions": self.skip_permissions, "timeout": self.timeout,
                  "data_dir": self.data_dir, "mcp_servers": self.mcp_servers}
        if self.app_data_dir is not None:
            kw["app_data_dir"] = self.app_data_dir
        if self.conversation_id:
            kw["conversation_id"] = self.conversation_id
        elif self.continue_latest:
            kw["continue_latest"] = True
        kw.update(kwargs)
        if self.chat:
            kw["enable_system_messages"] = False
        return Session(**kw)


def write_chat_agent(workspace: str, prompt: str = CHAT_PROMPT, name: str = CHAT_AGENT) -> str:
    """Write the chat-mode agent definition into ``workspace``; returns its path."""
    path: Final = os.path.join(workspace, ".agents", "agents", name, "agent.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(CHAT_AGENT_MD.format(name=name, prompt=prompt))
    return path
