# TaskSolver

A small, **provider-agnostic query flow for vision-language models**. You define a `TaskSpec` (a prompt plus the answer type that parses the model's reply), pick a backend with a model-id string, and call it — TaskSolver routes the request to the right provider and hands back a parsed answer with retry-on-parse-failure built in.

Originally built for [BlenderAlchemy](https://github.com/ianhuang0630/BlenderAlchemyOfficial)-style VLM systems; used by [3D-CoT](https://github.com/Wenri/3D-CoT).

## Backends

One `Agent`, many backends — selected by the `vision_model` id. Provider adapters are imported **lazily**, so constructing one backend does not initialize the others.

| Backend | Example model-ids | Transport |
| --- | --- | --- |
| OpenAI | `gpt-4o`, `gpt-4o-mini`, `o1`, `o3-mini` | OpenAI API (`chat.completions`) |
| Anthropic | `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5` | Anthropic Messages API |
| Claude Agent SDK | `claude-code`, `claude-code-sonnet-4-6`, `claude-agent-opus` | official Python SDK and its bundled Claude Code CLI (`pyclaude`); existing aliases retained |
| Antigravity CLI | `agy`, `antigravity`, `agy-gemini-3-pro` | bundled `agy` CLI and matching capture shim under a PTY (`pyagy`) |
| Codex SDK | `codex`, `codex-gpt-5-codex`, `codex-sdk` | official Python SDK over the linked Codex app-server (`pycodex`) |
| Kimi Code CLI | `kimi-code`, `kimi-code-k3` | vendored Node CLI under a PTY (`pykimi`), wirecap bridge via an N-API addon; built from source by `pixi install` |
| vLLM (OpenAI-compatible) | `qwen3`, `qwen3-5`, `qwen3-6` | OpenAI client with a custom `base_url` |
| Moonshot / Kimi (HTTP) | `kimi2-6`, `kimi-k2.7-code` | Anthropic-compatible endpoint (the HTTP `KimiModel`, distinct from the `kimi-code` CLI) |
| Gemini | `gemini-3-pro`, `gemini-3-flash`, `gemini-2.0-flash` | Google GenAI SDK |
| Local HuggingFace | `qwen`, `intern`, `minicpm`, `phi`, `llama` | in-process via `transformers` (needs the `local` extra) |

The agy/codex/kimi-code native artifacts embed CPython (the shim's interpreter, codex's libpython, the `wirecap_node` addon's libpython): a consuming environment must run the **same Python minor version** as the env they were built in (this repo pins 3.15) — a version-mismatched consumer is an unsupported configuration.

The agent runtime sources and binaries are pinned separately from TaskSolver's Python
dependencies:

| CLI | Pinned version | Integration and build notes |
| --- | --- | --- |
| Antigravity (`agy`) | **1.4.12** | [BuildID-matched shim and symbol map](antigravity/README.md) |
| Codex | **0.166.0** (`rust-v0.166.0`) | [Rust source capture and session-store compatibility](codex/README.md) |
| Kimi Code | **2.4.1** (`@moonshot-ai/kimi-code@2.4.1`) | [Node bundle, search worker, and native addon](kimi/README.md) |
| Claude Code | **2.1.316**, with Agent SDK **0.2.176** | [Vendored Python SDK and checksum-verified executable](claude/README.md) |

## Install

TaskSolver requires Python 3.15 and uses [pixi](https://pixi.sh) for its managed environment:

```bash
pixi install                                              # build + install the default env
pixi run python test_scripts/text_only.py --model claude-code
pixi shell                                                # drop into the environment
```

The pixi workspace is configured in `pyproject.toml`: it pins Python 3.15 and Node.js 26 and builds TaskSolver as a Linux x86-64 package via the `pixi-build` backend. A local package build compiles the Antigravity shim, Rust Codex binary, and Kimi Code bundle plus native addon. It also packages the checked-in Claude and Codex Python SDKs and fetches the pinned Claude Code executable after verifying its size and SHA256. A redistributable conda build (`pixi publish`) leaves the agy and Claude Code runtimes out; see [Redistributable bundles](#redistributable-bundles).

The workspace defines **two** environments: the **default** env (`pixi install`) carries the core API / CLI backends plus the UI/plotting tools — **no GPU required**; the **`cuda`** env (`pixi install -e cuda`) adds the local HuggingFace + torch (cu134) adapters and **flash-attn**, with the CUDA 13.4 toolkit (nvcc + headers) from conda-forge, so it needs an NVIDIA GPU whose driver supports CUDA 13.4 but no host CUDA toolkit, and builds flash-attn from source on first run (uv caches the wheel afterward). Node.js 26 is also pinned in the package's build environment because Kimi's frozen dependency graph excludes Node.js 25.

After editing adapters or updating vendored sources, refresh the installed package in the workspace that consumes it:

```bash
pixi reinstall --locked tasksolver
```

This rebuilds and recopies the bundled artifacts as needed. Omit `--locked` when dependency changes require a new lockfile. A consuming workspace's noneditable path dependency is an installed snapshot; source edits alone do not refresh its wheel.

### Redistributable bundles

The agy and Claude Code runtimes are built from proprietary upstream executables, so they never ship in a redistributable bundle. A non-editable conda build (`pixi publish`, or a conda source dependency) neither fetches nor packages `pyagy/vendor/{agy,libagy.so}` or `claude_agent_sdk/_bundled/`; Codex (Apache-2.0) and Kimi Code (MIT) ship as usual with their licenses. The editable workspace and wheels built by uv or pip for a path dependency still build and keep everything.

To put the two runtimes back into an environment that installed such a bundle, run the helper from a TaskSolver checkout at the same version:

```bash
pixi run reconstruct --target /path/to/env               # both, auto-detected
pixi run reconstruct --agy ~/Downloads/antigravity --claude=/path/to/claude --target /path/to/env
```

`--agy`/`--claude` take a path or, without one, auto-detect; with neither flag it restores both. `--target` accepts an environment prefix, a Python executable or a `site-packages` directory (default: the current environment). Inputs must be the exact pinned official builds (`antigravity/vendor.json`, `claude/vendor.json`). Auto-detection checks the checkout's build caches, `PATH`, `~/.local/bin` (including agy's `agy.*.old` updater backups), `~/.local/share/claude/versions`, `~/.claude/local`, Nix profiles (following their wrappers) and editor extensions' bundled `claude`. It reports every rejected candidate, for example a newer agy or a Nix-patched Claude Code. The helper then rebuilds with the checkout's normal build steps and verifies the copy it installs.

### Releases and runtime updates

`.github/workflows/runtime-updates.yml` runs twice a day (04:17 and 16:17 UTC), on pushes to `main` that change code (docs, Nix and CI files excluded), and on demand. Each run checks every separately released part: agy; Codex, whose release tag carries both the CLI and its Python SDK; the Claude Agent SDK and the Claude Code CLI, tracked independently; and Kimi Code, whose tag carries the CLI and its node SDK:

- **A new runtime release** is adopted with `tools/runtimes.py`, which replays TaskSolver's patches onto it with a three-way merge. The workflow then builds the workspace, runs the offline tests, builds the redistributable bundle, raises the patch version, tags and pushes, and publishes a GitHub **pre-release** of the latest code with the `.conda` attached.
- **A push with nothing to adopt** only builds and runs the offline tests.
- **A full release** is manual: run the workflow with `release` checked (Actions, *Runtime updates*, *Run workflow*, or `gh workflow run runtime-updates.yml -f release=true`). It adopts any outdated runtime first.

An adoption that needs hand work (a patch conflict, an agy release whose hooked functions changed, a Claude Code release whose code no longer matches the unbundler's source contracts or bun_graph's native signatures, a failing test) goes to a `runtime-updates/run-<id>` branch and an issue instead. While the issue is open, scheduled and push runs hold that runtime at the versions it names and still adopt the others; a newer release of the held runtime is tried again. The same steps run locally:

```bash
python3 tools/runtimes.py check                 # pinned vs latest, per SDK and CLI
python3 tools/runtimes.py adopt agy              # or codex, claude, kimi; --version X.Y.Z
python3 tools/runtimes.py adopt claude --cli-version X.Y.Z   # Claude Code alone, ahead of the SDK
pixi install --locked && pixi run python test_scripts/test_runtimes.py
```

On a hosted runner a build-and-test run takes about an hour and a pre-release about an hour and a half, plus up to two hours when `nix/hashes.json` needs refreshing (the Codex crates with their V8 checkout and the Kimi pnpm store, about 3.3 GB). Repository variables tune the workflow: `RUNTIME_RUNNER` (a JSON `runs-on` value, e.g. a self-hosted runner that keeps build caches between runs) and `PREFIX_CHANNEL` (also upload full releases to that prefix.dev channel through trusted publishing). The workflow keeps `nix/hashes.json` current but does not build the Nix package; the downstream Nix configuration that pins TaskSolver builds and tests it.

For à-la-carte use, the same groups are portable `[project.optional-dependencies]` extras — `tasksolver[local]` (torch + HuggingFace adapters, **including flash-attn**) and `tasksolver[app]` (UI/plotting) — so the package stays installable as a dependency by uv and pixi (a consumer points at its own torch index). Core dependency versions are resolved by the consuming workspace, with compatibility bounds for the agent SDKs and native capture dependencies; `flash-attn` has no prebuilt wheels for new Pythons and builds from source, so `tasksolver[local]` needs CUDA. Its newest PyPI release (2.8.3.post2) also compiles with C++17 and fails against torch >= 2.13, whose headers need C++20; TaskSolver's `cuda` env therefore forces C++20 for that build (`tools/cxx20-g++` plus `NVCC_APPEND_FLAGS`), as upstream's unreleased `main` does.

Credentials are supplied via a `KeyChain` (loading files like `system/credentials/openai_api.txt`) or environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `VLLM_API_KEY`, `MOONSHOT_API_KEY`).

### Nix

The flake builds TaskSolver for x86_64-linux on nixpkgs 26.05, with Python 3.15 and all four runtimes, and runs it without pixi:

```bash
nix run github:Wenri/TaskSolver -- agy codex claude kimi --web-port 7681   # the terminal screens
nix profile install github:Wenri/TaskSolver   # `tasksolver` and `tasksolver-python` on PATH
nix/update-hashes                             # after a runtime bump: refresh nix/hashes.json
```

The default package, `launcher` ([`nix/launcher.nix`](nix/launcher.nix)), has two commands on one Python environment: `tasksolver BACKEND...`, which opens the terminal screens as `pixi run tasksolver` does, and `tasksolver-python`, that environment's interpreter, for scripts (`tasksolver-python test_scripts/text_only.py --model claude-code`). The package depends on nixpkgs' tmux, ttyd (for `--web-port`) and Node 26 (for Kimi Code) by store path, so neither command changes `PATH`. The runtimes' embedded interpreters take the launching environment as `PYTHONHOME`, the role `CONDA_PREFIX` plays under pixi. `.#tasksolver` is the Python package alone, and `.#launcher.env` the environment.

The build fetches the agy and Claude Code releases pinned in `antigravity/vendor.json` and `claude/vendor.json` and derives `libagy.so` and Claude's runtime from them on the building machine, as the editable workspace does; Codex and Kimi Code build from the vendored sources. Since agy and Claude Code are proprietary, the package is marked unfree: the flake's own `packages` allow it, and a consuming configuration sets `nixpkgs.config.allowUnfree`. In another flake, apply `overlays.deps` first. It adds what TaskSolver needs above nixpkgs: the pinned Claude Agent SDK, and LIEF 1.x and fixes for the Python 3.15 package set, which nixpkgs 26.05 does not build yet. Then apply `overlays.default`, which adds `pkgs.tasksolver`, with its commands as `pkgs.tasksolver.launcher`. `package.nix` also takes a consumer's own builds of the pinned runtimes (`agyArchive`, `claudeExecutable`, `codexBase`). The first build takes hours, since no binary cache holds the Python 3.15 set and Codex compiles from source. `nix/hashes.json` holds the hashes the pins cannot carry (the Codex crates, the Kimi Code pnpm store, the rusty_v8 assets and the SDK source), and `nix/update-hashes` recomputes them: each entry records what it was computed from, so a run recomputes only the entries whose inputs moved (`--force` recomputes all).

## Verify the CLI integrations

From the TaskSolver checkout, these checks run without provider credentials or model calls:

```bash
pixi run typecheck

# SDKs and chat behavior
pixi run python test_scripts/test_claude_sdk.py
pixi run python test_scripts/test_claude_bundle.py
pixi run python test_scripts/test_codex_sdk.py
pixi run python test_scripts/test_codex_sdk_native.py
pixi run python test_scripts/test_chat_mode.py
pixi run python test_scripts/test_reconstruct.py   # redistributable bundles and the reconstruct helper
pixi run python test_scripts/test_runtimes.py      # the runtime updater, against local git fixtures

# Exec/PTY clients and packaged Kimi runtime
pixi run python test_scripts/test_codex_argv.py
pixi run python test_scripts/test_codex_process.py
pixi run python test_scripts/test_responses_decode.py
pixi run python test_scripts/test_kimi_argv.py
pixi run python test_scripts/test_kimi_decode.py
pixi run python test_scripts/test_kimi_process.py
pixi run python test_scripts/test_kimi_session.py
pixi run python test_scripts/test_kimi_bundle.py

# Shared process, native bridge and framing
pixi run python test_scripts/test_wire_session.py
pixi run python test_scripts/test_wire_process.py
pixi run python test_scripts/test_wire_bridge.py
pixi run python test_scripts/test_wirecap_framing.py

# Managed interactive screens (Pixi supplies tmux and ttyd)
pixi run python test_scripts/test_terminal.py
pixi run python test_scripts/test_tasksolver_terminal.py
pixi run python test_scripts/test_webterm.py
pixi run python test_scripts/test_webterm_views.py

# Agy build metadata, native readers and configuration
pixi run python test_scripts/test_agy_symbols.py
pixi run python test_scripts/test_agy_stubs.py
pixi run python test_scripts/test_appresponse.py
pixi run python test_scripts/test_config.py
pixi run python test_scripts/test_system_messages.py
pixi run python test_scripts/test_agy_system_message_queue.py
pixi run python test_scripts/test_agy_system_message_api.py
```

`typecheck` runs BasedPyright in strict mode over all project Python, including build
tools, examples and tests. Vendored sources and generated build directories are excluded.
Both inferred and explicit `Any` are errors. `typings/` declares the third-party APIs
used by build tools and optional decoders; JSON and process boundaries use explicit data types.
The six first-party runtime packages ship `py.typed` markers for downstream checking.

The Claude bundle check needs its downloaded runtime; the Kimi bundle check needs its built
artifacts and Node. They verify package contents and runtime behavior outside the checkout.
The native bridge and agy checks need the pixi C++ toolchain. Agy's stub checks exercise the
built host, including GC-stress startup. The Codex native SDK check uses a localhost Responses
fixture to verify capture, overlapping requests, resume, and synthetic API-key handling.
A skipped test does not verify an unbuilt artifact. See the backend READMEs for live smoke
tests, which require provider access. Offline SDK fixtures do not establish authenticated
provider behavior; agy's `test_client.py` includes a live request and is listed separately
in its backend README.

## Usage

Define an answer type (parses the raw reply; raise `GPTOutputParseException` to trigger a retry), wrap it in a `TaskSpec`, then query a backend:

```python
from tasksolver.common import TaskSpec, ParsedAnswer, Question, KeyChain
from tasksolver.exceptions import GPTOutputParseException

class HeadsOrTails(ParsedAnswer):
    def __init__(self, value: str) -> None: self.value = value
    @staticmethod
    def parser(raw: str) -> "HeadsOrTails":
        out = raw.strip().strip(".,").lower()
        if out not in ("heads", "tails"):
            raise GPTOutputParseException("expected `heads` or `tails`")
        return HeadsOrTails(out)
    def __str__(self) -> str: return self.value

task = TaskSpec(name="Coin Toss", description="Flip a fair coin; reply `heads` or `tails`.",
                answer_type=HeadsOrTails, followup_func=None, completed_func=None)

keys = KeyChain(); keys.add_key("claude_api_key", "system/credentials/claude_api.txt")

from tasksolver.claude import ClaudeModel
model = ClaudeModel(api_key=keys["claude_api_key"], task=task, model="claude-sonnet-4-6")

q = task.first_question(Question(["Toss the coin. What's the outcome?"]))
parsed, raw, meta, payload = model.rough_guess(q, max_tokens=2000)
print(parsed)
```

`rough_guess` retries parsing up to `max_tries` (default 1); if the reply still won't parse it raises `GPTMaxTriesExceededException`, which exposes the last attempt's `.raw_response`, `.response_metadata`, and `.request_payload` so you can inspect what the model actually returned.

To dispatch by model-id instead of importing an adapter directly, use `tasksolver.agent.Agent(api_key, task, vision_model="claude-code-sonnet-4-6")` and call `agent.visual_interface.run_once(question)`. Runnable text-only and vision examples live in [`test_scripts/`](test_scripts/).

### Managed terminal screens

Run the native agy, Codex, Claude and Kimi interfaces in named screens inside the
terminal that is already running Python:

```bash
pixi run tasksolver agy codex claude kimi
```

Without pixi, the Nix package's `tasksolver` command takes the same arguments
(`nix run github:Wenri/TaskSolver -- agy codex claude kimi`; see [Nix](#nix)).
Pixi and the Nix package supply [tmux](https://github.com/tmux/tmux/wiki/Getting-Started) and
[ttyd](https://github.com/tsl0922/ttyd) **1.7.4 or newer**.
TaskSolver owns a private tmux server; your existing tmux sessions are unaffected.
Press **Ctrl-b w** to choose a screen, **Ctrl-b n/p** for next/previous, and
**Ctrl-b d** to detach. The command above then exits and closes its screens.
Each backend keeps its normal login, workspace trust and tool approval UI. No prompt
is submitted during launch. `--workspace PATH`, `--model NAME` and `--no-capture`
configure the launch; a model name must be understood by every selected backend.
Terminal backends are exactly `agy`, `codex`, `claude` and `kimi`; model names are
forwarded unchanged, without `Agent`'s model-id alias expansion.

For application-controlled lifetimes, use the shared manager directly:

```python
from tasksolver.terminal import TerminalManager, open_terminal

with TerminalManager(workspace=".") as terminals:
    agy = open_terminal("agy", manager=terminals)
    codex = open_terminal("codex", manager=terminals)
    claude = open_terminal("claude", manager=terminals)
    kimi = open_terminal("kimi", manager=terminals)
    terminals.attach("agy")  # Uses this terminal; returns when you detach.
    print(agy.snapshot(history=100))
    # Screens still run here, until explicitly closed or the context exits.
```

`terminals.screens` exposes the named screens. Each screen has `send(text)`,
`submit(text)` (paste then Enter), `send_keys("C-c")`, `snapshot()`, `alive`,
`exit_status` and `close()`. Multiple screens may use the same backend; names default
to `agy`, `agy-2`, etc. `TerminalManager.open(name, argv)` also runs arbitrary
commands. The generic manager lives in `wirecap.runtime.terminal`; provider launch
configuration stays in `tasksolver.terminal`. It creates no desktop windows.

Agy, Codex and Kimi keep their linked JSONL capture, exposed as `screen.capture_path`.
Default capture files are temporary and removed when the manager closes. Pass
`capture="/absolute/path/run.jsonl"` to retain one, or `capture=False` to disable it.
Claude uses its own session store and has `capture_path=None`. Its bundled runtime
always includes the JavaScript bridge: `pyclaude.attach_terminal(screen)` connects
the official SDK to the same native UI process through `screen.control_socket`.
`pyclaude.ask` and `Session` use that runtime in headless SDK processes. See
[Claude native UI control](claude/README.md#sdk-control-of-the-native-ui).
CLIs may contact their services during startup, so captures can include account and
configuration metadata before any prompt is sent.

For browser viewing, run:

```bash
pixi run tasksolver agy codex claude kimi --web-port 7681
```

Each screen gets its own page: agy at port 7681, Codex at 7682, Claude at 7683 and
Kimi at 7684. Open the printed URLs as separate browser panels or tabs. Each page
stays on its screen; tmux navigation and its status bar are hidden there.
`--web-port 0` assigns available ports automatically.
When stdin and stdout are terminals, the launcher also
attaches the local terminal, so local and browser views run together. **Ctrl-b d**
detaches locally while keeping the screens and web pages running; **Ctrl-C** at the
launcher then closes them. Add `--no-attach` to `--web-port` for web-only operation.
Without a local TTY, web-only operation is automatic. The browser is read-only by default;
`--writable` enables typing into each page's agent. No browser is opened
automatically. To view a panel from another host, forward its port and open
`http://127.0.0.1:7681` on your computer:

```bash
ssh -N -L 7681:127.0.0.1:7681 user@host
```

The server binds only to `127.0.0.1` and checks WebSocket origins. Python can keep a
local and browser view open together, or serve a particular screen read-only:

```python
from tasksolver.terminal import TerminalManager, open_terminal
from wirecap.runtime.webterm import WebTerminal

with TerminalManager() as terminals:
    open_terminal("agy", manager=terminals)
    open_terminal("codex", manager=terminals)
    with WebTerminal(terminals, screen="codex", port=7681) as web:
        print(web.url, flush=True)
        terminals.attach("agy")
```

A screen-specific page links only that screen's tmux window, sharing its existing
process with the local terminal. Closing a page leaves the screen running; closing
a screen closes its viewers without affecting other pages. `WebTerminal(terminals)`
without `screen=` provides an all-screen viewer with independent navigation.
The manager can open new screens and viewers after the previous ones close. Closing the
manager closes everything it owns. `port=0` selects an available port. Outside Pixi,
install tmux and, for browser viewing, ttyd >=1.7.4 on `PATH`. Executables can be
selected with `tmux_bin=` / `ttyd_bin=` or the CLI's `--tmux-bin` / `--ttyd-bin`.

### Chat mode

`Agent(api_key, task, vision_model=..., chat=True)` runs the Claude, Codex and agy backends as plain chat calls, for example to use a subscription login as a judge: no MCP servers, skills, memory or project instructions, and thinking off (Claude) or lowest (Codex `effort="none"`, agy `--effort low`). Claude and Codex receive the Question's images inline and get no tools; agy cannot take inline images, so it gets the PNG files and only its `view_file` tool. A reply that uses any other tool, or opens a file it was not given, raises `tasksolver.cli_backend.ChatModeViolation`. `backend_options={"effort": ..., "system_prompt": ..., "timeout": ...}` passes backend settings through `Agent`. kimi-code has no chat mode. Details: the [Claude](claude/README.md#chat-mode), [Codex](codex/README.md#chat-mode) and [agy](antigravity/README.md#chat-mode-agymodelchattrue) READMEs; offline tests in `test_scripts/test_chat_mode.py`.

## Agent SDKs

The existing `tasksolver.claude_code.ClaudeCodeModel` now uses the official
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python).
`claude-code*` and `claude-agent*` select the same implementation. The SDK source
is checked in at `claude/vendor/sdk`; TaskSolver downloads its
pinned, checksum-verified CLI during a local package build, extracts its JavaScript and
installs the local bridge (`pixi run reconstruct --claude` does the same for a
redistributable bundle). A separate global CLI installation is unnecessary. Set
`ANTHROPIC_API_KEY`, pass a key (the `claude` service in a `KeyChain`), or use the
SDK-supported local authentication. Direct queries and resumable sessions are
available in `pyclaude`; see [Claude usage](claude/README.md).

The official [Codex Python SDK](https://learn.chatgpt.com/docs/codex-sdk#python-library)
is integrated into `CodexModel`, `pycodex.ask_sdk`, and `pycodex.SDKSession`.
Both `codex*` and `codex-sdk*` Agent IDs select the SDK transport.
`CodexModel(transport="exec")` and the lower-level `pycodex.ask`/`Session` retain
the linked exec and PTY interfaces. It uses the same
bundled linked binary and preserves wire captures alongside SDK results.
See [Codex usage](codex/README.md). The SDK source ships from the existing pinned
Codex vendor tree as `openai_codex`; TaskSolver supplies its runtime, avoiding a
second CLI download. Both SDKs are shipped as source inside TaskSolver;
neither is installed as a separate SDK distribution. Avoid separately installing `openai-codex` or
`claude-agent-sdk` into this environment because they own the same namespaces.

Both adapters keep TaskSolver's parsing retries and four-tuple return contract.
SDK sessions expose conversation IDs for resuming later turns. Tool permissions
are configurable; SDK configuration and session behavior are described in each
backend README.

## Task skills

[`.agents/skills/`](.agents/skills/) holds task skills in the `SKILL.md` format that **both** agy
and codex read — each is discovered by walking up from the working directory to the repo root, and
loaded lazily (only its name and description sit in context until the model uses it).
[`blender-gt-reconstruction`](.agents/skills/blender-gt-reconstruction/) reconstructs a ground-truth
3D model through a Blender MCP server, in either access mode (GT in the same scene, or behind a
read-only viewport-only server).

To use one in a throwaway workspace, seed it and pass that workspace to `ask()`:

```python
from wirecap.runtime.workspace import ensure_git_workspace
ws = ensure_git_workspace(skills=[".agents/skills/blender-gt-reconstruction"])
pycodex.ask("Reconstruct the GT model.", workspace=ws)     # or pyagy.ask(...)
```

See [`.agents/skills/README.md`](.agents/skills/README.md) for the layout, how the progressive
disclosure is split, how to verify a skill reached the model, and how to add one.

## MCP servers at session start

The CLI backends can register arbitrary MCP servers (`{name: {command, args,
env}}`) when a session starts. `wirecap/runtime/mcp.py` owns the canonical
spec normalization and the per-CLI config serializations (legacy claude
`--mcp-config`/`--strict-mcp-config`, codex `-c mcp_servers.<name>=<TOML>`,
kimi-code's discovered `mcpServers` JSON, qwen config fragments, the shared
`mcpServers` JSON writer), so harnesses that drive the CLIs directly can render
the exact same configs. The Claude SDK adapter passes MCP configuration directly
to `ClaudeAgentOptions`, including SDK-hosted and HTTP/SSE servers.

Through the backends:

```python
servers = {"blender": {"command": "uv",
                       "args": ["run", "--directory", "path/to/mcp", "blender-mcp"],
                       "env": {"BLENDER_MCP_PORT": "9999"}}}
Agent(key, task, vision_model="claude-code-sonnet-4-6",
      mcp_servers=servers, workspace=str(task_dir))   # also agy-*/codex*/kimi-code*
pyagy.Session(mcp_servers=servers, data_dir=str(private_home))   # scoped store
pycodex.ask("...", mcp_servers=servers)                     # rendered -c flags
pykimi.ask("...", mcp_servers=servers)                      # discovered mcp.json
```

agy specifics (`pyagy.write_mcp_servers` / `Session(mcp_servers=…)`): every
server command is wrapped `/usr/bin/env -u PYTHONHOME …` (agy hands its own
environment — including the launcher's `PYTHONHOME` — to the servers it
spawns, which breaks `uv run` interpreters); with `data_dir` the config goes
into that scoped home's own `config/mcp_config.json` (created real, not
symlinked to `~/.gemini/config`, so the run neither sees nor mutates the
user's global servers) and the scoped store is pre-onboarded
(`seed_onboarding`) so a fresh home doesn't hang in agy's first-run wizard.
Without `data_dir` the servers merge into the global store and are removed on
cleanup — don't run parallel sessions that way.

## Antigravity (`agy`) instrumentation

[`antigravity/`](antigravity/) links Google's Antigravity CLI (`agy`) in-process (converting the Go PIE binary into `vendor/libagy.so` and linking a C++23 host executable `vendor/agy` with bidirectional `ld` / `ld.so`-bound static cgocall trampolines plus an embedded CPython), and exposes `agy` as a TaskSolver backend (`pyagy.AgyModel`, mirroring `ClaudeCodeModel`). See [`antigravity/README.md`](antigravity/README.md) for the design, the static cgocall trampoline books (`src/gocall.cpp` + `src/gomod.cpp`), current build/validation notes, and the historical WSL1/cloud-kernel investigations.

`pyagy` also captures agy's own system notifications and interagent messages in
`AgyResponse.system_messages`, with capture errors in `.system_message_errors`.
`Session.send_system_message(text, conversation_id=...)` queues a native harness notification
for the main agent or a specific subagent's next model invocation. It does not wake an idle
agent or replace the provider's system prompt. See [system notifications](antigravity/README.md#system-notifications)
for capture, delivery status, and examples.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
