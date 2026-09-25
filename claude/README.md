# Claude Agent SDK

`pyclaude` is TaskSolver's Claude backend alongside `pycodex`, `pyagy`, and
`pykimi`. It uses Anthropic's official Python Agent SDK and its bundled Claude
Code executable. It does not need native instrumentation or a separate global CLI install.
The existing `claude-code` model aliases now use this backend; the direct
Anthropic Messages adapter remains separate.

The official Python SDK source is checked into `claude/vendor/sdk` at tag
`v0.2.159` (commit `2b87034f571b75797b976b3f32a6dbe7a03f20eb`), and packaged
as part of TaskSolver. There is no separate `claude-agent-sdk` package dependency.
`pixi install` builds from this source and bundles the matching Claude Code
2.1.281 executable from Anthropic's distribution, verified by size and SHA256.
The pins live in [`vendor.json`](vendor.json); [`build_cli.py`](build_cli.py)
checks the SDK's CLI version before downloading, reuses verified cached files,
and replaces the executable only after verification. The executable is packaged
under `claude_agent_sdk/_bundled/claude` and is not tracked in Git.
Override the executable path with
`sdk_options={"cli_path": "/path/to/claude"}` when needed.

After changing the wrapper or vendored SDK, run `pixi reinstall --locked tasksolver`
in the consuming workspace to refresh its installed copy. Omit `--locked` if
dependencies changed; see the [installation notes](../README.md#install).

## Authentication

Set `ANTHROPIC_API_KEY` in the calling environment, or pass `api_key=`. The SDK
also uses Claude Code's existing authentication and supported cloud-provider
configuration. TaskSolver does not read, copy, or rewrite credential files.
An API key takes precedence over an existing login in noninteractive execution.
Load `.env` files in your application; the SDK does not load them automatically.

## Calls and sessions

```python
from pyclaude import Session, ask, ask_async, ask_many

response = ask("What is 2 + 2?", model="sonnet", workspace=".", timeout=120)
print(response.text, response.session_id, response.usage)

samples = ask_many("Give one short title for a robotics paper.", 3)

with Session(model="sonnet", workspace=".") as session:
    first = session.ask("Remember that the project is called Atlas.")
    second = session.ask("What is the project called?")
    print(session.session_id, second.text)

# In async applications: response = await ask_async("What is 2 + 2?")
```

Each call owns the complete SDK client lifecycle. A `Session` reopens a client
and resumes its saved session ID for each follow-up; it does not keep a CLI
process alive between turns. Claude stores the transcript on disk.
`Session(session_id="...")` resumes a previously stored conversation.
`session.history()` returns this object's responses. Concurrent turns on the
same `Session` are rejected; independent sessions can run concurrently.
The synchronous API works when the calling thread already has an event loop,
although async applications should use `ask_async` to avoid blocking that loop.

`ClaudeResponse.text` comes from the terminal SDK result, with the final main
assistant message as a fallback. Intermediate narration is not mixed into the
answer. Responses retain session ID, native usage, cost, thinking blocks, SDK
messages, and terminal result metadata. `ClaudeAgentError.response` preserves
failed-turn diagnostics. Timeouts raise `TimeoutError` after SDK cleanup, with
partial diagnostics on its `response` attribute;
transport and authentication exceptions propagate from the SDK.
Partial timeout responses have `timed_out=True` and `exit_status=1`.
`Session` retains discovered session IDs and failed or timed-out responses in
its history, so an explicit follow-up can resume a turn interrupted by a deadline.

## Tools, MCP, and options

Standalone `ask` and `Session` default to the `Read` built-in tool, preapproved,
with the SDK's `default` permission mode. `tools=[]` disables built-in tools;
`tools=None` uses the SDK's full built-in toolset. `allowed_tools` preapproves
tool names and does not itself restrict availability. MCP tools remain available
when built-ins are restricted:

```python
response = ask(
    "Read issue 12",
    tools=["Read"],
    mcp_servers={"issues": {"command": "my-issue-server", "args": []}},
    allowed_tools=["Read", "mcp__issues__get_issue"],
    permission_mode="dontAsk",
)
```

Passing `mcp_servers` enables strict MCP configuration for that call. SDK stdio,
HTTP/SSE, and in-process MCP server configurations are accepted. Use
`sdk_options` for advanced options such as `system_prompt`, `max_turns`,
`max_budget_usd`, `thinking`, `effort`, hooks, or
`setting_sources=[]` to disable filesystem settings. Dedicated wrapper options
take precedence over corresponding SDK fields. `extra_env` is merged into the
SDK subprocess environment; `api_key` overrides its `ANTHROPIC_API_KEY` value.
On POSIX, stdio MCP commands use TaskSolver's shared environment wrapper to
unset `PYTHONHOME` for server interpreters.
Avoid putting credentials in prompts or response metadata.

## TaskSolver adapter and images

`pyclaude.ClaudeAgentModel` follows TaskSolver's existing four-value return
contract and retry behavior. `tasksolver.claude_code.ClaudeCodeModel` is its
compatibility entry point. For compatibility, the model constructor retains
`allowed_tools="Read"` and `permission_mode="acceptEdits"`; its default built-in
tool restriction follows `allowed_tools`. Pass `tools` explicitly to select
availability separately from preapproval rules.

The adapter sends images as native SDK content blocks in their original order
with text. It does not create temporary image files or require the model to read
an image from a local path. The lower-level client also accepts a list of
Anthropic text/image content blocks as its prompt. `max_tokens` remains in
TaskSolver's request payload for compatibility, but the Agent SDK offers no
per-answer `max_tokens` control; use `max_turns` or `max_budget_usd` to limit runs.

## Chat mode

`ClaudeAgentModel(chat=True)` (or `Agent(..., chat=True)`) runs each call as a plain
chat turn: no built-in tools, no MCP servers (strict config with none), no
filesystem settings, skills, auto-memory or CLAUDE.md, one turn, no saved
transcript and no session-title request; thinking is disabled and effort is
`"low"` unless `thinking=` / `effort=` say otherwise. Without a `workspace` the
CLI runs in a private empty directory. A reply with a tool call raises
`tasksolver.cli_backend.ChatModeViolation`. The request that reaches the API
then carries `tools: []`, the SDK's one-line system prompt (or `system_prompt=`)
and one user message: the Question's parts plus four Claude Code context
reminders (working directory, model, account email, date), which no setting
removes. The switches are `CHAT_ENV` and `CHAT_SDK_OPTIONS` in `pyclaude.model`;
they are CLI behaviour, so re-check them through a logging proxy
(`ANTHROPIC_BASE_URL`) after a CLI upgrade.

Every SDK child, chat mode or not, gets the parent Claude Code session's
variables (`pyclaude.client.PARENT_SESSION_ENV`: session id, messaging socket and
token, effort, ...) blanked, so a process started inside Claude Code does not
join that session.

## Verification

Run these checks from the TaskSolver checkout:

```bash
pixi run python test_scripts/test_claude_sdk.py
pixi run python test_scripts/test_claude_bundle.py
```

The SDK suite uses synthetic messages and the vendored SDK with a local protocol
stub. It covers aliases, images, parsing and metadata, session resume, MCP
permissions, concurrency, timeout cancellation and subprocess cleanup. The bundle
suite checks download verification with synthetic bytes, package contents, and
the built executable's `--version` outside the checkout. These checks make no
authenticated provider calls; the package check skips if the runtime is unbuilt.

Official references: [Python SDK](https://code.claude.com/docs/en/agent-sdk/python),
[authentication](https://code.claude.com/docs/en/authentication),
[streaming image input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode),
[permissions](https://code.claude.com/docs/en/agent-sdk/permissions).
