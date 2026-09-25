# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this is

TaskSolver is a **provider-agnostic query flow for vision-language models**. You define a `TaskSpec` (prompt + an answer type that parses the model's reply), select a backend by a model-id string, and call it; TaskSolver routes to the right provider adapter and returns a parsed answer with retry-on-parse-failure built in. Originally built for BlenderAlchemy-style VLM systems; consumed by external projects (3D-CoT, BlenderGym), so model-id aliases are part of the public contract — renaming them is a breaking change.

## Commands

```bash
# pixi: pins Python 3.13, builds tasksolver and its bundled agent runtimes.
# TWO envs (pyproject [tool.pixi.environments]): `default` = core + app (UI/plotting +
# SDKs and native CLI builds) — NO CUDA/torch, installs GPU-free; `cuda`
# = default + local (torch cu130 + flash-attn) — needs a CUDA 13 GPU and compiles
# flash-attn from source on first `pixi install -e cuda` (~25-30 min, cached after).
pixi install            # default env, GPU-free; `pixi install -e cuda` for the full GPU stack
pixi run python test_scripts/text_only.py --model claude-code
pixi shell                                             # interactive shell inside the env

# pymport (Node.js ↔ Python bridge) — dev diagnostic, not used by the package
# pymport embeds libpython3.13 inside Node.js so you can call Python libraries
# from JavaScript without subprocess overhead.  It is compiled from source
# against pixi's Python (--build-from-source) so the native addon links the
# same libpython that owns numpy/torch/etc.  The embedded interpreter needs
# PYTHONHOME=$CONDA_PREFIX to find conda's stdlib + site-packages; that is no
# longer a global activation var (only the agy/codex launchers set it per-child),
# so the `pymport-test` task carries it in its own `env`.  Recompile after a
# Python-version change.
pixi run npm-install                                   # compile pymport from source
pixi run pymport-test                                  # smoke test: prints Python + numpy versions

# Offline SDK/packaging tests: no provider credentials or model calls
pixi run python test_scripts/test_claude_sdk.py
pixi run python test_scripts/test_claude_bundle.py
pixi run python test_scripts/test_codex_sdk.py
pixi run python test_scripts/test_codex_sdk_native.py
pixi run python test_scripts/test_chat_mode.py        # chat mode, image files, vendored paths

# Live provider examples (credentials required)
python test_scripts/text_only.py --model claude-code   # choices: claude, claude-code, gpt, gemini, qwen, intern
pixi run -e cuda python test_scripts/vision_language.py # vision; uses QwenModel (torch → needs the `cuda` env)
```

`test_scripts/` contains automated offline tests (plain assertion scripts and `unittest` suites), native package checks, and separate live examples. Run the relevant offline tests after changes; they require no provider credentials. Package/native checks may need built artifacts, and a skip is not a passing runtime check. Do not describe these fixtures as authenticated provider validation. The pixi environment pins **Python 3.13**; native capture artifacts must match the consumer's Python minor version. Core dependencies are resolved by the host environment, with compatibility bounds for the agent SDKs and native capture dependencies.

**pixi-build env (`[tool.pixi.*]` in `pyproject.toml`):** the workspace uses the `pixi-build` preview feature. `tasksolver` is a Linux x86-64 conda package (`noarch = false`) built by `pixi-build-python` and depended on by path. The build compiles agy/Codex/Kimi capture integrations, packages the checked-in Claude and Codex SDKs, and fetches Claude's pinned size/SHA256-verified executable. The backend builds *without isolation*, so `[build-system].requires` (setuptools/pip) and native build tools must also appear under `[tool.pixi.package.host-dependencies]`. The backend resolves from pixi's default channel and is not pinned in `pixi.lock`.

After source changes, use `pixi reinstall --locked tasksolver` in the consuming workspace to rebuild and refresh the installed package; omit `--locked` when dependencies changed. In particular, a parent's noneditable PyPI path dependency remains an installed snapshot until refreshed. An editable Python source path also does not rebuild native artifacts by itself. Do not install duplicate `claude-agent-sdk`, `openai-codex`, or `openai-codex-cli-bin` distributions: TaskSolver owns their SDK namespaces and bundled runtimes. Keep `claude/build_cli.py` and `claude/vendor.json` in `MANIFEST.in` so source distributions can execute the build hook.

**Optional extras, the merged env, and building flash-attn.** `[project.optional-dependencies]` keeps two *portable* extras — `local` (torch/HF stack **incl. flash-attn**, since the intern/phi/minicpm/llama adapters hardcode `flash_attention_2`) and `app` (streamlit/flask/matplotlib). TaskSolver's workspace maps them to **two** pixi environments (`[tool.pixi.environments]`): `default` = `app` only (GPU-free — what plain `pixi install` resolves; verified to install without a GPU), and `cuda` = `local` + `app`. The cu130 torch index, `no-build-isolation`, and the flash-attn build toolchain live in `[tool.pixi.feature.local.*]`; the CUDA 13 requirement is a virtual package on the workspace `platforms` but **only binds the `cuda` env**. So `pixi install -e cuda` (or `pip install tasksolver[local]`) requires CUDA 13 and compiles flash-attn; plain `pixi install` does not. That build has two non-obvious knobs, set in `[tool.pixi.feature.local.activation.env]` (pixi *does* apply a feature's `activation.env` during the build): use **`FLASH_ATTN_CUDA_ARCHS`** (e.g. `"80"` = Ampere) — flash-attn silently ignores `TORCH_CUDA_ARCH_LIST` and otherwise builds a 4-arch fat binary (~4× the work); and set **`MAX_JOBS`** explicitly, else flash-attn's `psutil`-based auto-calc under-parallelizes because this host's ZFS ARC cache deflates "available" memory.

## Architecture

### One Agent, many backends — lazy string dispatch
`tasksolver/agent.py` `Agent.__init__` is a big `if/elif` over the `vision_model` string. The matching branch **lazily imports** that provider's adapter and stores an instance on `self.visual_interface`, avoiding initialization of other backends. To dispatch by id, construct `Agent(api_key, task, vision_model=...)` and call `agent.visual_interface.run_once(question)`. Model-id → backend map (with alias normalization) lives entirely in this method; e.g. `claude-code-sonnet-4-6` → Claude SDK model `claude-sonnet-4-6`, `gemini-3-pro` → `gemini-3-pro-preview`.

### The backend adapter contract (duck-typed, with one shared base for the CLI backends)
The direct API adapters (`GPTModel`, `ClaudeModel`, `VLLMModel`, `KimiModel`, `GeminiModel`, and the local HF ones `QwenModel`/`InternModel`/`MiniCPMModel`/`PhiModel`/`LlamaModel`) are standalone classes that independently implement the same surface. There is no ABC enforcing it — match the existing shape exactly when adding one.

The four **agent-runtime** backends share `CLIBackendModel` (`tasksolver/cli_backend.py`): `ClaudeCodeModel`, `pyagy.AgyModel`, `pycodex.CodexModel` and `pykimi.KimiCodeModel`. The base owns the parsing retry loops, exception context, `run_once`, and default payload/call handling. `ClaudeCodeModel` is a compatibility subclass of `pyclaude.ClaudeAgentModel`; it supplies native SDK image payloads and SDK response metadata. `CodexModel` defaults to the official Python SDK and app-server, with native image inputs and capture metadata; `transport="exec"` selects its compatibility client. Agy and Kimi retain their instrumented PTY clients.

For another agent-runtime backend, subclass this base and set `backend_label`, `command_label`, `generic_model_aliases`, `no_output_hint`, and `_client_ask_many`; override payload/call/finish methods when the protocol requires it. Direct HTTP adapters remain standalone because they share no subprocess/workspace machinery. `CLIBackendModel` lives in `tasksolver/`, never `wirecap/`, whose stdlib-import purity is enforced by the `python3 -S` probes. Import the base only from provider `model.py` modules; keep those behind the packages' PEP-562 lazy exports so embedded CLI interpreters do not import TaskSolver.

The three instrumented CLIs share `wirecap` but differ in how the bridge is hosted: agy injects a C++ shim into its closed Go binary through the ELF loader's `--preload`; Codex compiles the bridge into its Rust binary; Kimi loads it as an N-API addon (`kimi/native/wirecap_node.node`) through source hooks. Their exec/PTY clients use `WirePtyPopen` and `mp_child`, with capture JSONL authoritative. The Codex SDK instead drives app-server through JSON-RPC pipes and combines SDK results with native capture. Kimi's addon promotes libpython to the global namespace (`dlopen(RTLD_GLOBAL|RTLD_NOLOAD)`) before `wire_start()`, allowing Python's C extensions to resolve the C API.

- `__init__(api_key, task, model=...)`
- `prepare_payload(question, max_tokens, ...)` *(a staticmethod on the HTTP adapters; a **classmethod** on `CLIBackendModel`, so the vision preamble and error text follow the subclass — callable either on the class or an instance)* → provider-specific request dict
- `ask(payload, n_choices=1)` → `(messages, metadata)`
- `rough_guess(question, max_tokens, max_tries=1, ...)` → the **4-tuple** below, wrapping the retry loop
- `run_once(question, max_tokens)` → calls `self.task.first_question(question)` then `rough_guess`
- `many_rough_guesses(num_threads, question, ...)` → a 4-tuple containing parsed-answer, message and metadata lists plus the shared payload; concurrency depends on the backend

**Canonical return everywhere is the 4-tuple `(parsed_answer, raw_response, metadata, payload)`.** Note `rough_guess` expects an already-assembled question, while `run_once` assembles the full task prompt first via `first_question` — test scripts sometimes call `first_question()` themselves and then `rough_guess()`, which is equivalent to `run_once()`.

### Task definition triad — `TaskSpec` + `Question` + `ParsedAnswer` (all in `common.py`)
- **`ParsedAnswer`** subclass: defines `parser(raw: str)` (raise `GPTOutputParseException` to trigger a retry) and `__str__`. This is the per-task output contract.
- **`TaskSpec`**: bundles `name`, `description`, `answer_type`, `followup_func`, `completed_func`, plus optional `background` and `examples`. `first_question()` assembles description + background + examples + the user question into a single `Question`.
- **`Question`**: an ordered list of **tagged multimodal elements** (str, `PIL.Image`, `Path`, `URL`, `ParsedAnswer`, or a nested `Question`). Supports `+`, prepend/append, tag-based filtering (`eval(filter_tag=...)` / `subquestion(...)`), and `get_json()` which normalizes everything to a provider-neutral content list that each adapter's `prepare_payload` translates. Image elements carry the live `PIL.Image` under an `"image"` key that adapters strip before sending.

### Retry-on-parse-failure (the universal loop)
Every `rough_guess`/`many_rough_guesses` runs: `ask` → `answer_type.parser(content)` → on `GPTOutputParseException`, retry up to `max_tries` (**default 1**), else raise `GPTMaxTriesExceededException`. Parsing — not the HTTP call — is what drives retries. That exception carries the failed attempt's context — `.raw_response`, `.response_metadata`, `.request_payload` — so callers see *what* failed to parse, not just that retries ran out (mirroring the metadata attached to successful answers below); pass those through when you raise it from an adapter.

### Response metadata attachment
`attach_response_metadata` (common.py) decorates the parsed answer with `.llm_response_metadata`, `.request_payload`, and an extracted `.explicit_reasoning_output` — `extract_explicit_reasoning_output` digs reasoning/thinking traces out of varied provider response shapes (Anthropic thinking blocks, vLLM `reasoning_content`, etc.). Preserve this when touching adapters; downstream consumers read these attributes.

### Credentials — `KeyChain` and the service-name gotcha
`KeyChain.add_key(service, key)` stores `key` literally, or reads the first line if `key` is an existing file path. **Gotcha:** when you pass a `KeyChain` into `Agent`, the service names it looks up are `openai`, `claude`, `gemini`, `vllm`, `moonshot` — NOT the `*_api_key` names used in the README/test-script examples. Those examples work only because they index the KeyChain to a *string* (`api_dict['claude_api_key']`) and pass it straight to an adapter, bypassing `Agent`'s lookup. If you wire a KeyChain through `Agent`, name the services `openai`/`claude`/`gemini`/`vllm`/`moonshot`.

Env-var fallbacks resolved inside the adapters (see `vllm.py`, `kimi.py`): vLLM uses `VLLM_API_KEY` + a base URL from `QWEN3_OPENAI_BASE_URL`/`QWEN3_BASE_URL`/`VLLM_OPENAI_BASE_URL`/`VLLM_BASE_URL` (unless a builtin endpoint like `qwen3-5`/`qwen3-6` is selected, which hardcodes both); Kimi uses `MOONSHOT_API_KEY` against the fixed `https://api.kimi.com/coding`.

### Claude Agent SDK and Codex SDK

`tasksolver/claude_code.py` preserves `ClaudeCodeModel` as a subclass of
`pyclaude.ClaudeAgentModel`. Both `claude-code*` and `claude-agent*` aliases route
here; HTTP `claude-*` IDs still select `ClaudeModel`. The official SDK source is checked in at `claude/vendor/sdk`; TaskSolver bundles
its pinned, checksum-verified CLI and the SDK handles authentication,
permissions, MCP and typed messages. Explicit keys use `KeyChain["claude"]` or
child `ANTHROPIC_API_KEY`; otherwise SDK-supported authentication applies.
`claude/pyclaude` adds direct queries and resumable sessions; the complete SDK
client lifecycle must stay in one async task. Do not use the PTY `WireSession`
base for this protocol. Preserve terminal result errors, session ID, usage,
cost and reasoning in metadata. See `claude/README.md`,
`test_scripts/test_claude_sdk.py` and `test_scripts/test_claude_bundle.py`.

`codex*` and `codex-sdk*` IDs select `CodexModel`, whose default transport is
now the SDK. Explicit `transport="exec"` and low-level `pycodex.ask`/`Session`
retain the exec/PTY transport. `pycodex.SDKSession` drives the official Python SDK's typed
threads over app-server JSON-RPC with native capture. The pristine SDK package
`openai_codex` ships from `codex/vendor/sdk/python/src`, matching the pinned CLI
source. Do not add a duplicate pip SDK/runtime dependency. Both SDK transports
keep the canonical TaskSolver four-tuple. See `codex/README.md`,
`test_scripts/test_codex_sdk.py` and `test_scripts/test_codex_sdk_native.py`.

### Chat mode — an agent runtime as a plain chat call

`Agent(..., chat=True)` (or `chat=True` on `ClaudeAgentModel` / `CodexModel` / `AgyModel`) turns
the Claude, Codex and agy backends into chat-completion equivalents, e.g. to use a subscription
login as a judge. The agent gets no MCP servers, skills, memory or project instructions, and
thinking is off (Claude: `thinking` disabled, `effort="low"`) or lowest (Codex: `effort="none"`,
agy: `--effort low`). Claude and Codex take the Question as ordered inline text/image parts
(`prepare_payload(inline=True)`: no image files, no vision preamble) and get no tools; agy cannot
take inline images, so it gets the PNG files and one tool to open them (`view_file`). A reply
that used any other tool, or opened a file it was not given, raises `ChatModeViolation`
(`_chat_violations` per backend). `backend_options={...}` passes constructor overrides
(`effort`, `thinking`, `system_prompt`, `timeout`, ...). kimi-code raises on `chat=True`.

- **Claude** (`pyclaude.model.CHAT_ENV`, `CHAT_SDK_OPTIONS`): `tools=[]`, `--strict-mcp-config`
  with no servers, `setting_sources=[]`, `skills=[]`, one turn, `--no-session-persistence`, a
  named session (no per-call title request), a private empty cwd. What remains of Claude Code
  is the one-line SDK system prompt and four context reminders (cwd, model, account email, date).
  `pyclaude.client` also blanks the parent session's variables (`PARENT_SESSION_ENV`) in every
  SDK child, chat or not — otherwise a child started inside Claude Code joins the parent's
  session id, messaging socket and effort.
- **Codex** (`pycodex.model.CHAT_CONFIG`, `CHAT_ENV`): an ephemeral thread with empty (or
  `system_prompt`) base instructions, feature/instruction overrides, `CODEX_EXEC_SERVER_URL=none`
  (no execution environment, which is what removes shell/apply_patch/view_image), each MCP
  server of `$CODEX_HOME/config.toml` disabled (an override cannot delete a user server), images
  as `ImageInput` data URLs, capture off.

- **agy** (`pyagy.model.CHAT_AGENT_MD`): a workspace agent (`.agents/agents/tasksolver-chat/agent.md`,
  run with `--agent`) with `tools: [view_file]`, `excludeDefaultComponents: true` (no default
  prompt sections or built-in tools) and `inheritCustomizations: false` (no user skills, rules,
  plugins, subagents or MCP servers); the harness still offers `manage_task`, which has nothing
  to manage. `trust=False` keeps the workspace out of the user's global `trustedWorkspaces`, and
  `CHAT_ENV` unsets `SSH_*` (with them agy skips its keyring login; `extra_env` values of `None`
  now unset). Needs agy >= 1.2.11 (earlier releases ignore workspace agents under `--print`).

All three were checked against the real requests (a logging proxy for Anthropic, the native
capture for Codex and agy): no tools (agy: `view_file` + `manage_task`), one user message,
thinking/reasoning as configured. Re-check after a
CLI upgrade — the switches are CLI behaviour, not SDK contract. Offline tests:
`test_scripts/test_chat_mode.py`.

**Image files for file-reading agents.** `Question.get_json(save_local=True, save_dir=...)` writes
PNG (lossless — thin annotations survive) under `save_dir`, default `$TASKSOLVER_IMAGE_DIR` or
`<tmp>/tasksolver-images`; `CLIBackendModel.prepare_payload` saves into `<workspace>/.tasksolver-images`
when a workspace is set and announces absolute paths. (It used to write JPEGs to a CWD-relative
`temporary/`, which an agent running in another workspace could not resolve.)

### TAORI agent loop (scaffolding — mostly unused today)
`Agent` also exposes a higher-level **think / act / observe / reflect / interject** loop backed by an `EventCollection` of typed `Event`s (`event.py`: `ThinkEvent`, `ActEvent`, `EvaluateEvent`, …). `act`, `observe`, and `run` are `@abstractmethod` — intended to be subclassed per environment/task. Current real usage drives `visual_interface.run_once()` / `rough_guess()` directly and does not exercise this loop; treat it as an extension point, not load-bearing code.

### MCP provisioning at session start (`wirecap/runtime/mcp.py`)

The canonical MCP server spec is a plain dict `{"command", "args", "env"}`; `wirecap/runtime/mcp.py`
owns normalization (`normalize_server_spec`, the idempotent `env_wrapped`) and the per-CLI
serializations (`codex_config_flags` — bare-TOML-key names enforced, `claude_mcp_args`,
`qwen_mcp_servers`, `kimi_mcp_json`/`kimi_mcp_toml_lines`, `write_mcp_servers_json` with merge
semantics). It is parent-side runtime code (never imported from `wirecap.decode`). The backends
thread `mcp_servers=` through: `Agent(mcp_servers=, workspace=)` → `ClaudeCodeModel` (SDK `ClaudeAgentOptions.mcp_servers`, workspace honored as cwd), `AgyModel`/`pyagy.ask/Session`
(`pyagy.write_mcp_servers` into the scoped or global store; `prepare_scoped_home(...,
link_global_config=False)` + `seed_onboarding` make a scoped home self-contained and
already-onboarded; commands get the `/usr/bin/env -u PYTHONHOME` wrap so agy-spawned servers can
start their own interpreters), `CodexModel`/`pycodex.ask/Session` (rendered `-c` flags via
`pycodex.mcp_flags` — same PYTHONHOME unwrap as pyagy, for the same reason; the SDK transport supplies equivalent app-server configuration), and
`KimiCodeModel`/`pykimi.ask` (kimi-code has no MCP flag — `pykimi.config.mcp_json` writes the
`mcpServers` document to the workspace-local `.kimi-code/mcp.json` the CLI auto-discovers, same
PYTHONHOME unwrap). Non-CLI backends raise on `mcp_servers`/`workspace`. Offline tests:
`test_scripts/test_mcp_serializers.py`, `test_scripts/test_kimi_argv.py`.

pycodex additionally carries the harness-shaped one-shot controls on `ask()` — `codex_home=`
(store scoping, honored by the session readers too), `session_id=`/`continue_latest=`
(non-interactive `codex exec resume`; the returned `CodexResponse.session_id` is store-read so a
silently-forked new thread is visible), `prompt_via_stdin=` (fd 0 = an unlinked temp file,
`codex exec -`), `capture=` (path override), and `CodexResponse.timed_out`. `WirePopen.close` is
group-aware for its instrumented subprocesses: leader SIGTERM → bounded reap → `killpg` sweep (PTY children are
session leaders, so the sweep hits exactly the CLI's own MCP/tool children — on success paths
too), and pidfd-sentinel popens close their PTY master via `_teardown_fds`. Offline tests:
`test_scripts/test_codex_argv.py` (argv/env/sessions/mcp_flags) and
`test_scripts/test_codex_process.py` (stub-binary end-to-end: stdin round-trip, timeout, group
sweep, fd stability).

## Agent runtime subsystems

Four backends have their own package roots. Agy, Codex and Kimi share the
instrumentation layer; Claude uses its SDK protocol directly:

- **`wirecap/`** — the capture layer shared by agy, Codex and Kimi.
  - `wirecap/decode/` is **stdlib-import-pure** (it is imported by the CPython interpreter embedded
    inside the instrumented CLI): the JSONL `Recorder`, HTTP/1.1+SSE framing, `BaseCorrelator`,
    HTTP/2 reassembly, the `TurnBuilder`/`Usage` contract, and `mp_child` (the in-host
    multiprocessing child). `BaseCorrelator` accumulates each response under
    `TurnBuilder.stream_key` and lets `TurnBuilder.request_matches` narrow the time-based
    pairing: agy's builder keys by `responseId` and matches the request model to the served
    `modelVersion`, because agy streams its session-title call alongside the answer turn (one
    shared accumulator merged them into one turn paired with the title request). Builders
    without a key (codex, kimi) behave as before. agy turns keep Gemini thought parts in
    `reasoning`, apart from the answer `text`, and the shim emits a TLS write larger than its
    16 KB copy buffer as several events (it used to emit the first 16 KB only, so no request
    carrying tool declarations or images ever decoded). Never import `wirecap.runtime` or `tasksolver` from here — the
    `python3 -S` probes in `test_scripts/` (one per instrumented CLI's dispatch module, plus the
    decode-layer probe) enforce it, and they are the tripwire for any move.
  - `wirecap/runtime/` is parent-side: `WirePopen`/`WireProcess`, the PTY flavours
    `WirePtyPopen`/`WirePtyProcess`, git-workspace scoping, the vendored-artifact resolver.
  - `wirecap/native/` is the C ABI bridge (`libwirecap_bridge.a`) that embeds CPython on a
    16 MB-stack worker thread; linked into the agy shim, the codex binary, and the kimi-code addon.
- **`antigravity/`** → the `pyagy` package: Google's Go `agy` CLI, instrumented by a preloaded
  C++23 shim (`antigravity/src/`) that patches cgocall trampolines over recovered Go addresses.
  See `antigravity/README.md`.
- **`codex/`** → the `pycodex` package: OpenAI's Rust codex, built from a vendored source tree with
  the bridge compiled in (no preload needed). See `codex/README.md`.
- **`kimi/`** → the `pykimi` package: Moonshot's Node kimi-code, built from a vendored source tree
  with a three-line wiretap patch that loads the bridge as an N-API addon (`kimi/native/`). See
  `kimi/README.md`.
- **`claude/`** → the `pyclaude` wrapper and the pristine `claude/vendor/sdk` source.
  `claude/vendor.json` pins SDK v0.2.159 and Claude Code 2.1.281;
  `claude/build_cli.py` verifies and stages the executable under the SDK's
  `_bundled/` directory before packaging. See `claude/README.md`.

The compatibility PTY paths are symmetric — `agy --print` ≡ `codex exec` ≡ `kimi -p`, and all run
under the same PTY machinery and stream turns home over the same mp-child channel — so a change to
one usually belongs in `wirecap/` rather than duplicated. The multi-turn side is symmetric too:
`pyagy.Session` ≡ `pycodex.Session` ≡ `pykimi.Session` all ride
`wirecap/runtime/session.py` — `ask_turn` (the persistent turn loop, returning *why* the turn
settled: turn/idle/deadline/exit) plus the `WireSession` base (lazy start, dead-process guard,
id latching, `session_id`/`conversation_id` aliasing, opt-in `capture_tail`); providers supply
only process construction and response shape. Session processes must be built with
`max_wait=None` (asserted in `_start`): a Session's life IS the CLI's life, and a numeric
deadline would kill the bridge mid-session on wall time. Offline coverage:
`test_scripts/test_wire_session.py` (the base), `test_scripts/test_kimi_session.py` (kimi shell
mode: no `-p`, bracketed-paste submits, the pre-seeded workspace-trust record).

`pycodex.SDKSession` and `pyclaude.Session` have separate SDK lifecycles; do not
route them through `WireSession`. See their backend READMEs for the persistent
app-server versus resume-per-call behavior.

Run top-level tests with `pixi run python test_scripts/<file>.py`. Offline
coverage includes protocol decoding, MCP/configuration, response accessors,
sessions, process cleanup, package integrity and import-purity probes. Live
agy/Codex examples are separate and require authentication.

## Adding a new backend

1. Create `tasksolver/<name>.py` with a class implementing the adapter contract above (copy the closest existing adapter — `gpt4v.py` for OpenAI-compatible, `claude.py` for Anthropic-style — and keep the 4-tuple return + retry loop + `attach_response_metadata`). For a **CLI-subprocess** backend, subclass `tasksolver/cli_backend.py`'s `CLIBackendModel` instead of copying an adapter — it already supplies the payload assembly, retry loop and 4-tuple.
2. Add an `elif vision_model in (...)` branch to `Agent.__init__` with a **lazy** `from .<name> import <Class>` inside the branch. The `# TODO: Add your own model here` comments mark the spot.
3. If it takes a credential, decide its `KeyChain` service name and/or env-var fallback and follow the resolver pattern in `vllm.py`/`kimi.py`.
