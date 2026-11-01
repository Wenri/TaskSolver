# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this is

TaskSolver is a **provider-agnostic query flow for vision-language models**. You define a `TaskSpec` (prompt + an answer type that parses the model's reply), select a backend by a model-id string, and call it; TaskSolver routes to the right provider adapter and returns a parsed answer with retry-on-parse-failure built in. Originally built for BlenderAlchemy-style VLM systems; consumed by external projects (3D-CoT, BlenderGym), so model-id aliases are part of the public contract — renaming them is a breaking change.

## Commands

```bash
# pixi pins Python 3.15 and builds tasksolver with its bundled agent runtimes. Two envs:
# `default` (core + app: UI/plotting, SDKs, native CLI builds; GPU-free) and `cuda` (default +
# local: torch cu134, conda-forge CUDA 13.4, flash-attn compiled on the first
# `pixi install -e cuda`, ~25-30 min; needs an NVIDIA driver for CUDA 13.4).
pixi install            # default env; `pixi install -e cuda` for the GPU stack
pixi run python test_scripts/text_only.py --model claude-code
pixi shell

# pymport (Node ↔ Python): a dev diagnostic only. 1.5.1 calls Py_GetPythonHome, removed in 3.15,
# so it does not build until upstream ports it; rebuild with `npm rebuild pymport`.
pixi run npm-install
pixi run pymport-test

# Offline SDK/packaging tests: no credentials or model calls
pixi run python test_scripts/test_claude_sdk.py
pixi run python test_scripts/test_claude_bundle.py
pixi run python test_scripts/test_codex_sdk.py
pixi run python test_scripts/test_codex_sdk_native.py
pixi run python test_scripts/test_chat_mode.py        # chat mode, image files, vendored paths

# Live provider examples (credentials required)
python test_scripts/text_only.py --model claude-code   # claude, claude-code, gpt, gemini, qwen, intern
pixi run -e cuda python test_scripts/vision_language.py # QwenModel needs torch (the `cuda` env)
```

Run the relevant offline tests in `test_scripts/` after changes; they need no credentials. A skip is
not a pass, and offline fixtures are not authenticated provider validation. Native capture artifacts
must match the consumer's Python minor version (3.15).

**pixi-build.** `tasksolver` is a linux-64 conda package built by `pixi-build-python` without
isolation, so `[build-system].requires` and the native tools must also be in
`[tool.pixi.package.host-dependencies]`. Under rattler-build `CONDA_PREFIX` is the empty build env:
`setup.py` (`native_build_env`) points native builds at `$PREFIX` and passes `PY3CONFIG`.
`BuildPyNative` re-raises native failures as `SystemExit`, since the editable install would
otherwise only warn. A fresh build work dir has its own `HOME`, so codex's crates re-download and
rebuild (~25 min).

**Toolchain.** Conda GCC `>=15,<16` (C++23; GCC 15's constexpr asm strings build agy's stubs).
After a compiler-major upgrade reconfigure the native CMake trees from scratch, but keep
`antigravity/build/antigravity-*.raw`.

After source changes run `pixi reinstall --locked tasksolver` in the consuming workspace (without
`--locked` if dependencies changed); an editable source path does not rebuild natives. Never
install duplicate `claude-agent-sdk`, `openai-codex` or `openai-codex-cli-bin` distributions, and
keep `claude/build_cli.py` and `claude/vendor.json` in `MANIFEST.in`.

**flash-attn (`cuda` env).** flash-attn 2.8.3.post2 compiles as C++17, which torch >= 2.13
rejects, so `CXX` is `tools/cxx20-g++` and `NVCC_APPEND_FLAGS=-std=c++20`; drop both once a newer
release reaches PyPI. `[tool.pixi.feature.local.activation.env]` (applied during builds) sets
`CUDA_HOME`/`CUDA_INC_PATH`, blanks `NVCC_PREPEND_FLAGS`, prepends `/run/opengl-driver/lib` to
`NIX_LD_LIBRARY_PATH` on NixOS, and sets `FLASH_ATTN_CUDA_ARCHS` (it ignores
`TORCH_CUDA_ARCH_LIST`) and an explicit `MAX_JOBS` (ZFS ARC skews its memory estimate). The intern/
phi/minicpm/llama adapters hardcode `flash_attention_2`.

## Architecture

### One Agent, many backends — lazy string dispatch
`Agent.__init__` (`tasksolver/agent.py`) is an `if/elif` over the `vision_model` string; the matching
branch lazily imports its adapter and stores an instance on `self.visual_interface`, so other
backends never initialize. Dispatch by id with `Agent(api_key, task, vision_model=...)` and
`agent.visual_interface.run_once(question)`. The id → backend map, including alias normalization,
lives entirely in this method (e.g. `claude-code-sonnet-4-6` → SDK model `claude-sonnet-4-6`,
`gemini-3-pro` → `gemini-3-pro-preview`).

### The backend adapter contract (duck-typed, with one shared base for the CLI backends)
The direct API adapters (`GPTModel`, `ClaudeModel`, `VLLMModel`, `KimiModel`, `GeminiModel`, and the
local `QwenModel`/`InternModel`/`MiniCPMModel`/`PhiModel`/`LlamaModel`) implement the same surface
independently; no ABC enforces it, so match the existing shape exactly.

The four **agent-runtime** backends — `ClaudeCodeModel`, `pyagy.AgyModel`, `pycodex.CodexModel`,
`pykimi.KimiCodeModel` — share `CLIBackendModel` (`tasksolver/cli_backend.py`), which owns the
parse-retry loops, exception context, `run_once` and default payload/call handling.
`ClaudeCodeModel` subclasses `pyclaude.ClaudeAgentModel` (native SDK image payloads and metadata);
`CodexModel` defaults to the official Python SDK over app-server (`transport="exec"` selects the
compatibility client); agy and Kimi keep instrumented PTY clients. A new runtime backend subclasses
the base, sets `backend_label`, `command_label`, `generic_model_aliases`, `no_output_hint` and
`_client_ask_many`, and overrides payload/call/finish methods as its protocol needs.
`CLIBackendModel` lives in `tasksolver/`, never `wirecap/` (whose stdlib purity the `python3 -S`
probes enforce); import it only from provider `model.py` modules, behind the packages' PEP-562 lazy
exports, so embedded CLI interpreters never import TaskSolver.

The three instrumented CLIs host the `wirecap` bridge differently: agy converts its Go PIE into
`libagy.so` linked by a C++ host (`vendor/agy`); Codex compiles it into its Rust binary; Kimi loads
an N-API addon (`kimi/native/wirecap_node.node`) through a source patch, promoting libpython to the
global namespace (`dlopen(RTLD_GLOBAL|RTLD_NOLOAD)`) before `wire_start()` so Python's C extensions
resolve the C API. Their exec/PTY clients use `WirePtyPopen` and `mp_child`, with the capture JSONL
authoritative; the Codex SDK drives app-server over JSON-RPC and merges SDK results with capture.

- `__init__(api_key, task, model=...)`
- `prepare_payload(question, max_tokens, ...)` → provider request dict (a staticmethod on the HTTP
  adapters; a **classmethod** on `CLIBackendModel`, so the vision preamble and errors follow the subclass)
- `ask(payload, n_choices=1)` → `(messages, metadata)`
- `rough_guess(question, max_tokens, max_tries=1, ...)` → the 4-tuple, wrapping the retry loop
- `run_once(question, max_tokens)` → `self.task.first_question(question)`, then `rough_guess`
- `many_rough_guesses(num_threads, question, ...)` → a 4-tuple of parsed-answer, message and
  metadata lists plus the shared payload; concurrency depends on the backend

**The canonical return everywhere is `(parsed_answer, raw_response, metadata, payload)`.**
`rough_guess` takes an assembled question; `run_once` assembles it first via `first_question`.

### Task definition triad — `TaskSpec` + `Question` + `ParsedAnswer` (all in `common.py`)
- **`ParsedAnswer`** subclasses define `parser(raw: str)` (raise `GPTOutputParseException` to retry)
  and `__str__`: the per-task output contract.
- **`TaskSpec`** bundles `name`, `description`, `answer_type`, `followup_func`, `completed_func` and
  optional `background`/`examples`; `first_question()` assembles them with the user question.
- **`Question`** is an ordered list of tagged multimodal elements (str, `PIL.Image`, `Path`, `URL`,
  `ParsedAnswer`, nested `Question`) with `+`, prepend/append, tag filtering
  (`eval(filter_tag=...)`, `subquestion(...)`) and `get_json()`, a provider-neutral content list each
  adapter translates. Image elements carry the live image under `"image"`; adapters strip it.

### Retry-on-parse-failure
`ask` → `answer_type.parser(content)` → on `GPTOutputParseException` retry up to `max_tries`
(**default 1**), else raise `GPTMaxTriesExceededException`. Parsing, not the HTTP call, drives
retries. The exception carries `.raw_response`, `.response_metadata` and `.request_payload`; pass
them when raising it from an adapter.

### Response metadata
`attach_response_metadata` (`common.py`) sets `.llm_response_metadata`, `.request_payload` and
`.explicit_reasoning_output` (extracted from Anthropic thinking blocks, vLLM `reasoning_content`,
etc.) on the parsed answer. Downstream consumers read these; preserve them in adapters.

### Credentials — `KeyChain` and the service-name gotcha
`KeyChain.add_key(service, key)` stores `key`, or the first line of the file it names. **`Agent`
looks up `openai`, `claude`, `gemini`, `vllm`, `moonshot`** — not the `*_api_key` names in the
README examples, which index the KeyChain themselves and pass a string straight to an adapter.
Adapter env fallbacks: vLLM uses `VLLM_API_KEY` and a base URL from `QWEN3_OPENAI_BASE_URL` /
`QWEN3_BASE_URL` / `VLLM_OPENAI_BASE_URL` / `VLLM_BASE_URL` (builtin endpoints such as
`qwen3-5`/`qwen3-6` hardcode both); Kimi uses `MOONSHOT_API_KEY` against `https://api.kimi.com/coding`.

### Claude Agent SDK and Codex SDK
`claude-code*` / `claude-agent*` select `ClaudeCodeModel`; HTTP `claude-*` ids select `ClaudeModel`.
The official SDK is checked in at `claude/vendor/sdk` with a pinned, checksum-verified CLI. Keep the
whole SDK client lifecycle in one async task, never on `WireSession`, and preserve terminal result
errors, session id, usage, cost and reasoning in metadata. Explicit keys come from
`KeyChain["claude"]` or the child's `ANTHROPIC_API_KEY`.

`codex*` / `codex-sdk*` select `CodexModel`, SDK transport by default (`transport="exec"` and
`pycodex.ask`/`Session` keep exec/PTY). `openai_codex` ships from `codex/vendor/sdk/python/src`,
matching the pinned CLI; never add a duplicate pip SDK or runtime. See `claude/README.md` and
`codex/README.md`.

### Chat mode — an agent runtime as a plain chat call
`Agent(..., chat=True)` (or `chat=True` on `ClaudeAgentModel` / `CodexModel` / `AgyModel`) makes the
backend a chat-completion equivalent, e.g. a subscription login as a judge: no MCP servers, skills,
memory or project instructions; thinking off or lowest (Claude: disabled, `effort="low"`; Codex:
`effort="none"`; agy: `--effort low`). Claude and Codex take the Question as ordered inline
text/image parts (`prepare_payload(inline=True)`) and get no tools; agy cannot take inline images,
so it gets PNG files and `view_file`. Using any other tool, or opening a file it was not given,
raises `ChatModeViolation` (`_chat_violations`). `backend_options={...}` passes constructor
overrides (`effort`, `thinking`, `system_prompt`, `timeout`, ...). kimi-code raises on `chat=True`.

- **Claude** (`pyclaude.model.CHAT_ENV`, `CHAT_SDK_OPTIONS`): `tools=[]`, `--strict-mcp-config` with
  no servers, `setting_sources=[]`, `skills=[]`, one turn, `--no-session-persistence`, a named
  session (no title request), a private empty cwd. What remains is the one-line SDK system prompt and
  four context reminders (cwd, model, account email, date). `pyclaude.client` blanks the parent
  session's variables (`PARENT_SESSION_ENV`) in every SDK child, or a child started inside Claude
  Code joins the parent's session, messaging socket and effort.
- **Codex** (`pycodex.model.CHAT_CONFIG`, `CHAT_ENV`): an ephemeral thread with empty (or
  `system_prompt`) base instructions, `CODEX_EXEC_SERVER_URL=none` (no execution environment, so no
  shell/apply_patch/view_image), each `$CODEX_HOME/config.toml` MCP server disabled, images as
  `ImageInput` data URLs, capture off.
- **agy** (`pyagy.model.CHAT_AGENT_MD`): a workspace agent with only `view_file`, run with
  `trust=False` and without `SSH_*`; see `antigravity/README.md`. Needs agy >= 1.2.11.

Re-check against real requests after CLI upgrades: these switches are CLI behaviour, not SDK
contract. Tests: `test_scripts/test_chat_mode.py`.

**Image files.** `Question.get_json(save_local=True, save_dir=...)` writes lossless PNG under
`save_dir` (default `$TASKSOLVER_IMAGE_DIR` or `<tmp>/tasksolver-images`);
`CLIBackendModel.prepare_payload` uses `<workspace>/.tasksolver-images` when a workspace is set and
announces absolute paths.

### TAORI agent loop (scaffolding)
`Agent` also has a think / act / observe / reflect / interject loop over typed `Event`s
(`event.py`); `act`, `observe` and `run` are abstract. Real usage calls `run_once()` /
`rough_guess()` directly, so treat the loop as an extension point.

### MCP provisioning (`wirecap/runtime/mcp.py`)
The canonical server spec is `{"command", "args", "env"}`; `wirecap/runtime/mcp.py` (parent-side)
normalizes it and serializes it per CLI. `Agent(mcp_servers=, workspace=)` reaches Claude (SDK
`mcp_servers`), agy (`pyagy.write_mcp_servers` into the scoped or global store), Codex (`-c` flags
or app-server config) and Kimi (`.kimi-code/mcp.json`). Server commands are wrapped in
`/usr/bin/env -u PYTHONHOME` so they can start their own interpreters. Non-CLI backends raise on
`mcp_servers`/`workspace`. Tests: `test_mcp_serializers.py`, `test_kimi_argv.py`.

`pycodex.ask()` also takes `codex_home=`, `session_id=` / `continue_latest=` (`codex exec resume`;
the returned id is read from the store, so a silent fork shows), `prompt_via_stdin=` and
`capture=`. `WirePopen.close` terminates the whole process group, which reaches exactly the CLI's
own MCP and tool children. Tests: `test_codex_argv.py`, `test_codex_process.py`.

## Agent runtime subsystems

Agy, Codex and Kimi share the instrumentation layer; Claude speaks its SDK protocol directly.

- **`wirecap/`**, the shared capture layer:
  - `wirecap/decode/` is **stdlib-import-pure**, because each CLI's embedded interpreter imports it:
    the JSONL `Recorder`, HTTP/1.1+SSE framing, `BaseCorrelator`, HTTP/2 reassembly, the
    `TurnBuilder`/`Usage` contract and `mp_child`. agy's `TurnBuilder` keys responses by
    `responseId` and matches the request model to the served `modelVersion`, because agy streams
    its session-title call alongside the answer. Never import `wirecap.runtime` or `tasksolver`
    here; the `python3 -S` probes in `test_scripts/` enforce it.
  - `wirecap/runtime/` is parent-side: `WirePopen`/`WireProcess`, the PTY flavours, git-workspace
    scoping and the vendored-artifact resolver.
  - `wirecap/native/` is the C ABI bridge (`libwirecap_bridge.a`) that embeds CPython on a 16 MB-stack
    worker thread, linked into the agy host, the codex binary and the kimi addon. They build with
    GCC LTO (`TASKSOLVER_LTO`, default on); the archive holds fat LTO objects because codex's Rust
    link cannot read GCC bytecode.
- **`antigravity/`** → the `pyagy` package: Google's Go `agy` converted into `vendor/libagy.so`
  and linked into a C++23 host, `vendor/agy`; the mechanism is in `antigravity/README.md`. Rules:
  - `BOOKS` in `procdef.h` is the one table (`{id, mode, retcap, AGY_FULLGO(symbol)}`, or
    `AGY_ASMGO` / `AGY_OFF`); run `pixi run shim-books` after editing it. Generated
    `symbols_gen.h` holds `AGY_BUILD_ID` and the `AGY_STUBS` list; never edit it.
  - Keep eager PLT binding (lazy resolution would run on the Go stack and clobber R10/R11),
    `FuncFlagAsm` on the synthetic functions, `__builtin_unreachable()` after the stub asm,
    `goframe.cpp` at `-fno-lto`, and `g_agy_on` `used`/`externally_visible` (LTO cannot see the
    asm's loads). The host exports only what libagy.so references; do not add
    `--export-dynamic`.
  - Run `test_scripts/test_agy_stubs.py` after stub or toolchain changes.
  - `symbols/` is a source build package run as `python3 -m symbols.<tool>` from `antigravity/`;
    it needs LIEF, stays out of the wheel, and must not leak into the embedded runtime's
    stdlib-only modules. Every rebuild converts the cached raw agy (`build/antigravity-*.raw`,
    keyed by SHA512) from scratch; `shim-symbols` refreshes it, and `shim-agy-update` prints new
    pins for `CMakeLists.txt`.
- **`codex/`** → the `pycodex` package: OpenAI's Rust codex built from a vendored tree with the bridge
  compiled in. See `codex/README.md`.
- **`kimi/`** → the `pykimi` package: Moonshot's Node kimi-code built from a vendored tree, with a
  three-line patch loading the bridge as an N-API addon (`kimi/native/`). See `kimi/README.md`.
- **`claude/`** → `pyclaude` and the pristine `claude/vendor/sdk`. `claude/vendor.json` pins SDK
  v0.2.176 and Claude Code 2.1.316; `claude/build_cli.py` verifies and stages the executable under
  the SDK's `_bundled/` before packaging. See `claude/README.md`.

The PTY paths are symmetric (`agy --print` ≡ `codex exec` ≡ `kimi -p`), so a change to one usually
belongs in `wirecap/`. `pyagy.Session`, `pycodex.Session` and `pykimi.Session` share
`wirecap/runtime/session.py` (`ask_turn` and the `WireSession` base); providers supply only process
construction and response shape. Build session processes with `max_wait=None` (asserted in
`_start`), or a deadline kills the bridge mid-session. `pycodex.SDKSession` and `pyclaude.Session`
have their own SDK lifecycles and never use `WireSession`.

**agy system notifications.** `AgyResponse.system_messages` captures native system-step
serialization, including agy's own notifications. `Session.send_system_message()` queues a
`HookSystemMessage` for the target conversation's next `PreInvocation`; it does not wake idle
agents. Delivery uses a scoped command hook in `.agents/hooks.json`, preserving unrelated
configuration. Queue emission and observed serialization are distinct. The `system_message`
and `system_message_legacy` BOOKS entries cover both serializers; repeated history observations
are retained, truncation is explicit, and no conversation id is inferred from a Go pointer.
Malformed packets surface in `AgyResponse.system_message_errors`, including streamed capture.
These notifications have harness attribution, not provider system-prompt priority. Side events
must not start/reset the session's model-answer idle timer. Chat-mode sessions disable injection.
Offline checks: `test_system_messages.py`, `test_agy_system_message_queue.py`, and
`test_agy_system_message_api.py`. The authenticated `test_agy_system_messages_live.py` checks
native observations and outgoing requests; model compliance is not its delivery assertion.

Run tests with `pixi run python test_scripts/<file>.py`; live agy/Codex examples need authentication.

## Adding a new backend

1. Create `tasksolver/<name>.py` with a class implementing the adapter contract above (copy the closest existing adapter — `gpt4v.py` for OpenAI-compatible, `claude.py` for Anthropic-style — and keep the 4-tuple return + retry loop + `attach_response_metadata`). For a **CLI-subprocess** backend, subclass `tasksolver/cli_backend.py`'s `CLIBackendModel` instead of copying an adapter — it already supplies the payload assembly, retry loop and 4-tuple.
2. Add an `elif vision_model in (...)` branch to `Agent.__init__` with a **lazy** `from .<name> import <Class>` inside the branch. The `# TODO: Add your own model here` comments mark the spot.
3. If it takes a credential, decide its `KeyChain` service name and/or env-var fallback and follow the resolver pattern in `vllm.py`/`kimi.py`.
