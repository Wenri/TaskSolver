# Claude Agent SDK

`pyclaude` is TaskSolver's Claude backend alongside `pycodex`, `pyagy`, and
`pykimi`. It uses Anthropic's official Python Agent SDK with a bundled Claude
Code runtime whose JavaScript is extracted and wired to a local bridge. All
launches use this runtime; no separate global CLI install is needed.
The `claude-code` and `claude-agent` model aliases select this backend; the direct
Anthropic Messages adapter remains separate.

Run `pixi run python -m tasksolver.terminal claude` for Claude's native UI, with
optional [browser viewing](../README.md#managed-terminal-screens).
`pyclaude.attach_terminal(screen)` connects an SDK client to that same process.
`ask` and `Session` start headless SDK processes instead. Resuming a saved session
ID starts another process; attaching to a screen controls the running one.

The pristine official Python SDK is checked into `claude/vendor/sdk` at the tag
pinned in [`vendor.json`](vendor.json) and packaged as part of TaskSolver. Do not install a separate `claude-agent-sdk` distribution.
The SDK itself uses its existing subprocess `stream-json` protocol for headless
calls. Native UI attachment uses the SDK's custom transport interface and a
private Unix socket. Both paths execute the same bridged runtime.
TaskSolver requires that bundle and rejects `cli_path` overrides pointing elsewhere;
a missing bundle is an error rather than a fallback to a global CLI.

## Build and editable JavaScript

`pixi install` builds the runtime automatically, using
[`bun_graph`](https://github.com/Wenri/patchnar/blob/main/src/bun_graph.cc) from
`PATH`. Provide that executable in the build environment. [`vendor.json`](vendor.json)
pins the Claude Code release that SDK expects; [`build_cli.py`](build_cli.py) verifies the download's size
and SHA256, checks the SDK's CLI version, then stages the complete runtime under
`claude_agent_sdk/_bundled/`:

- `bun`: the private Bun executable with its embedded application graph removed.
- `claude`: a small native launcher that starts the external application and preserves native helper dispatch.
- `source/`: patched JavaScript, the local bridge, and matching `.jsc` sidecars.
- `assets/`: extracted file and text assets; text adapters preserve the original loader behavior.
- `modules/`: extracted native `.node` addons.
- `libwire.node` and `cache.mjs`: the native cache emitter and its JavaScript driver.
- `manifest.json`: source pins, generated hashes, native patches and build fingerprint.

The pristine executable is cached under `claude/build/cache/` as build input.
Generated outputs are not tracked in Git. Rebuild the bundle directly with
`pixi run python -m claude.build_cli`. For an isolated development copy:

```bash
pixi run python -m claude.unbundle --output claude/build/editable
claude/build/editable/claude --version
claude/build/editable/claude
```

Choose a new output directory; the tool refuses an existing path. Move the whole
runtime directory together. Source imports, workers and browser helpers resolve
relative to it. The entry loader disables upstream auto-updates; update the
package's pin and rebuild to change Claude versions.

Removing `.bun` removes the original JavaScript, bytecode and assets from the
executable and shrinks its load mapping. Statically linked native helpers such as
ripgrep and bfs remain in the private runtime; the launcher preserves their
`argv[0]` dispatch. Native `.node` modules are separate files. Prelinked JavaScript
records are module metadata, not native addons; the external loader reconstructs
its module records from the current source.

The pinned Bun sidecar loader treats external source as Latin-1. The build redirects
that one checked call to a small UTF-8 conversion shim, preserving Unicode and raw
template contents. The manifest records the five changed instruction bytes and the
added executable section. Other native sections, RELRO and stack settings are
preserved and independently checked.
ELF edits use pyelftools' named header structures; pwntools assembles the relative
call and locates binutils. The converter controls placement so existing native
sections stay at their original addresses.

The cache driver loads `libwire.node` through Bun's N-API support and invokes the
private runtime's own JSC emitter after all source patches. It copies and releases
the returned native cache, then writes Bun's length/hash footer. Sidecars live
beside their source because that is where Bun's loader reads them. Cache hits,
relocation, and rejection after source edits are checked against the real runtime.
JavaScript can call the addon's `validate()` and `emitModule(origin, sourceBuffer)`
directly; `emitModule` returns raw JSC bytes. Use `generateCaches` from `cache.mjs`
to add the source pragma and sidecar framing required by Bun's loader.
Text assets use UTF-8 files with small CommonJS adapters: ordinary loose Markdown
loading would otherwise render HTML or return a module namespace instead of text.

Claude's private Bun is necessary for this pin's native UI: stock Bun lacks
`Bun.ant.CellSegmenter` and other private APIs. This builds on the earlier Nix
`claude-code-graph.nix` work and the
[recovery tooling](https://github.com/Wenri/claude-code/tree/main/recovery).
The current JavaScript comes from the pinned executable.

Every build installs checked JavaScript insertions that register the native
session controls and forward real engine SDK frames before UI filtering. A changed
upstream insertion point fails the build. The interactive UI automatically creates
a private socket, announces it in the terminal and emits one machine-readable
`[tasksolver-claude-control]` JSON line to stderr. Managed screens use a socket in
the manager's private directory, exposed as `screen.control_socket`.
Headless SDK calls keep their stdio channel and create no UI socket.

After source changes, run `pixi reinstall --locked tasksolver` in the consuming
workspace to refresh installed artifacts. Omit `--locked` if dependencies changed;
see the [installation notes](../README.md#install).

Redistributable (conda) bundles leave `_bundled/` out because it is derived from the
proprietary executable. `pixi run reconstruct --claude [PATH]` rebuilds it from the
pinned official executable found on the machine (see the
[installation notes](../README.md#redistributable-bundles)).

### SDK control of the native UI

Use `open_terminal("claude", manager=manager)`, then
`pyclaude.attach_terminal(screen)` for an official SDK client connected to that
process. `attach_terminal` also accepts a socket path. The factory takes no SDK launch options: configure
the model, workspace and tool permissions in the native UI or at launch.

This example attaches the existing terminal while Python sends a query. Complete
the normal startup/trust screens first; after the answer, Ctrl-b d detaches the
view and finishes the example.

```python
import asyncio
from pyclaude import attach_terminal
from tasksolver.terminal import TerminalManager, open_terminal

async def main() -> None:
    responses: list[object] = []
    with TerminalManager(workspace=".") as manager:
        screen = open_terminal("claude", manager=manager)
        view = asyncio.create_task(asyncio.to_thread(manager.attach, screen.name))
        try:
            async with attach_terminal(screen, connect_timeout=300) as client:
                await client.query("Briefly describe this project.")
                async for message in client.receive_response():
                    responses.append(message)
            await view
        finally:
            manager.close()
            await view
    for message in responses:
        print(message)

asyncio.run(main())
```

The same screen can have a `WebTerminal` viewer. Browser input requires its
explicit `writable=True` option. The terminal owns permission dialogs; SDK
attachment never preapproves tools.

The local channel supports text queries, initialization metadata and interruption
of the caller's active turn. It rejects images, slash commands, SDK callback
hooks and other controls explicitly. One SDK query may be pending at a time;
queries submitted during a human turn fail. Human input queued during an SDK
turn remains a separate native turn. Responses are routed by the native input
UUID and engine identity, so a human result cannot complete the SDK query.
Closing the SDK client leaves the UI and its running turn alive. A new client
cannot claim the disconnected client's unfinished result.

Offline checks: `test_claude_unbundle.py`, `test_claude_transport.py` and
`test_claude_native.py` in `test_scripts/`; run the JavaScript lifecycle checks
with `pixi run node --test test_scripts/test_claude_native_bridge.mjs`.
Provider authentication and live model behavior require separate validation.

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
`session.history()` returns this object's responses. Constructor SDK options that
select a new or latest conversation apply to the first turn; later turns resume
the learned ID. Concurrent turns on the same `Session` are rejected; independent
sessions can run concurrently.
The synchronous API works when the calling thread already has an event loop,
although async applications should use `ask_async` to avoid blocking that loop.

`ClaudeResponse.text` comes from the terminal SDK result, with the final main
assistant message as a fallback. Intermediate narration is not mixed into the
answer. Responses retain session ID, native usage, cost, thinking blocks, SDK
messages, and terminal result metadata. `ClaudeAgentError.response` preserves
failed-turn diagnostics. Timeouts raise `ClaudeAgentTimeout` (a `TimeoutError`
subclass) after SDK cleanup, with partial diagnostics on its `response` attribute.
Transport, authentication and external cancellation exceptions propagate unchanged.
Partial timeout responses have `timed_out=True` and `exit_status=1`.
`Session` retains discovered session IDs and partial responses after failure,
timeout or cancellation, so an explicit follow-up can resume the conversation.

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
pixi run python test_scripts/test_chat_mode.py
```

The SDK suite uses synthetic messages and the vendored SDK with a local protocol
stub. It covers aliases, images, parsing and metadata, session resume after errors
or cancellation, MCP permissions, concurrency, deadlines and subprocess cleanup.
The bundle suite checks download verification with synthetic bytes, package contents, and
the built executable's `--version` outside the checkout. These checks make no
authenticated provider calls; the package check skips if the runtime is unbuilt.

Official references: [Python SDK](https://code.claude.com/docs/en/agent-sdk/python),
[authentication](https://code.claude.com/docs/en/authentication),
[streaming image input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode),
[permissions](https://code.claude.com/docs/en/agent-sdk/permissions).
