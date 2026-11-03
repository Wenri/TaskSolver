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
pixi run typecheck      # strict checks for all project Python; no Any, vendors excluded

# pymport (Node ↔ Python): a dev diagnostic only. 1.5.1 calls Py_GetPythonHome, removed in 3.15,
# so it does not build until upstream ports it; rebuild with `npm rebuild pymport`.
pixi run npm-install
pixi run pymport-test

# Restore agy and Claude Code into an env whose redistributable bundle omits them
pixi run reconstruct [--agy [PATH]] [--claude [PATH]] [--target ENV]

# Upstream runtimes: pinned vs latest, then adopt one (agy | codex | claude | kimi)
python3 tools/runtimes.py check
python3 tools/runtimes.py adopt agy [--version X.Y.Z]

# Offline SDK/packaging tests: no credentials or model calls
pixi run python test_scripts/test_claude_sdk.py
pixi run python test_scripts/test_claude_bundle.py
pixi run python test_scripts/test_reconstruct.py     # bundle modes and the reconstruct helper
pixi run python test_scripts/test_runtimes.py        # the runtime updater (local git fixtures)
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

**Python typing.** All first-party Python, including build tools, examples and tests, is checked
by BasedPyright in strict mode. Inferred and explicit `Any` are errors; do not add type-ignore
suppression. Use typed protocol boundaries for untyped dependencies, and keep the consumed API
stubs in `typings/` aligned with their pinned versions. Runtime packages ship `py.typed`; the
source-only third-party stubs stay out of the wheel. The package requires Python >=3.15.

**pixi-build.** `tasksolver` is a linux-64 conda package built by `pixi-build-python` without
isolation, so `[build-system].requires` and the native tools must also be in
`[tool.pixi.package.host-dependencies]`. Under rattler-build `CONDA_PREFIX` is the empty build env:
`setup.py` (`native_build_env`) points native builds at `$PREFIX` and passes `PY3CONFIG`.
`BuildPyNative` re-raises native failures as `SystemExit`, since the editable install would
otherwise only warn. A fresh build work dir has its own `HOME`. During conda builds,
`_build_codex` defaults `CARGO_HOME` to the checkout's `.pixi/cargo-home`, preserving
dependency sources (including the V8 crate) alongside the compiled artifacts in
`codex/vendor/codex-rs/target`. An explicit `CARGO_HOME` takes precedence.

**Redistributable bundles.** agy and Claude Code are built from proprietary upstream executables.
A non-editable conda build (`pixi publish`, a conda source dependency; `redistributable_bundle`
in `setup.py`) neither fetches nor ships them: `tasksolver.reconstruct.LOCAL_ONLY`
(`pyagy/vendor/{agy,libagy.so}`, `claude_agent_sdk/_bundled`) stays out, and a staged copy fails
the build. The editable workspace and uv/pip path wheels build and keep everything. Codex links
the standalone `wirecap/native/build` bridge, so it never needs agy. `pixi run reconstruct`
restores both runtimes: it verifies the pinned official executables (an explicit `--agy`/`--claude`
path, else the checkout caches, `PATH`, `~/.local/bin` with agy's `*.old` backups, Claude's
version store, Nix wrappers' targets, editor extensions), seeds the build caches, runs the same
`build_agy`/`build_claude` steps and installs into `--target` after checking it matches the
checkout (agy's BOOKS mirror, the SDK's CLI version, the Python minor). Codex (Apache-2.0 with
`NOTICE`) and kimi-code (MIT) ship in bundles; keep their files in `license-files`.

**Runtime updates.** Each runtime has one pin file: `antigravity/vendor.json`, `claude/vendor.json`,
`codex/vendor.json`, `kimi/vendor.json`. `tools/runtimes.py` (stdlib only) compares them with
upstream (`check`) per separately released part (agy, codex's tag with its CLI and Python SDK, the
Claude Agent SDK and Claude Code's latest channel, kimi-code's tag with its CLI and node SDK) and
adopts a release (`adopt`; `--cli-version` pins Claude Code alone). agy re-pins its archive and
extracted binary. The restore helper couples a target to the checkout by Agent SDK version.
The vendored trees replay TaskSolver's patch onto the new tag by a three-way merge of trees (base:
the pinned tag, ours: `HEAD`'s committed tree, theirs: the new tag), so commit vendored edits
first. Conflicted files keep their markers and are named; codex's `Cargo.lock` takes upstream's
copy, which the unlocked build completes. `docs` keeps the README pin table and pycodex's
`client_version` in step with the pins. `.github/workflows/runtime-updates.yml` runs twice a day,
on code pushes to `main` and on demand: adopt, `pixi install --locked`, `nix/update-hashes` when an
input of `nix/hashes.json` changed since the commit that last wrote it, the offline tests, `pixi publish` checked by
`tools/check_bundle.py`, then `bump`, an atomic push of the commit and tag, and a GitHub release
with `notes`. An adoption publishes a pre-release of the latest code; a push with nothing to adopt
only builds and tests; a full release is a manual dispatch with `release`. A failed adoption goes
to branch `runtime-updates/run-<id>` plus an issue titled by `runtimes.py title`. While it is open,
`check --held` holds only its runtimes at the versions it names; the others still adopt, and a newer
release of a held runtime is tried again. A Claude bump needs no port while upstream keeps the code
shapes that `claude/unbundle.py`'s source contracts and `bun_graph --natives`' signatures describe;
a miss fails the build naming the contract or entry point. The workflow does not build the Nix
package; the downstream Nix configuration that pins TaskSolver does.

**Toolchain.** Conda GCC `>=15,<16` (C++23; GCC 15's constexpr asm strings build agy's stubs).
After a compiler-major upgrade or removal of a cached build environment, reconfigure the
native CMake trees (`antigravity/build`, `wirecap/native/build`, `kimi/native/build`) from
scratch, but keep `antigravity/build/antigravity-*.raw`. `CMakeCache.txt` keeps the compiler path
it first found, so a stale tree fails with `goframe.cpp: #error "agy stubs require GCC 15 …"` even
when the build environment has GCC 15.

After source changes run `pixi reinstall --locked tasksolver` in the consuming workspace (without
`--locked` if dependencies changed); an editable source path does not rebuild natives. Never
install duplicate `claude-agent-sdk`, `openai-codex` or `openai-codex-cli-bin` distributions, and
keep Claude's build scripts, `native_bridge.mjs` and `vendor.json` in `MANIFEST.in`.

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
compatibility client); agy and Kimi keep linked PTY clients. A new runtime backend subclasses
`CLIBackendModel[ResponseType]`, sets `backend_label`, `command_label`, `generic_model_aliases`
and `no_output_hint`, and implements `_sample(payload, n_choices)` with its typed client options.
The existing `_client_ask_many` plus `_call_kwargs` extension remains supported. Override
payload/finish methods as the protocol needs.
`CLIBackendModel` lives in `tasksolver/`, never `wirecap/` (whose stdlib purity the `python3 -S`
probes enforce); import it only from provider `model.py` modules, behind the packages' PEP-562 lazy
exports, so embedded CLI interpreters never import TaskSolver.

The three linked CLIs host the `wirecap` bridge differently: agy converts its Go PIE into
`libagy.so` linked by a C++ host (`vendor/agy`); Codex compiles it into its Rust binary; Kimi loads
an N-API addon (`kimi/native/build/wirecap_node.node` in the source checkout) through a source patch, promoting libpython to the
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

**Interactive screens.** `tasksolver.terminal.open_terminal` launches the ordinary
agy/Codex/Claude/Kimi UI in `wirecap.runtime.terminal.TerminalManager`, using a private
tmux server. Pixi supplies tmux and ttyd >=1.7.4; non-Pixi environments must provide
these executables on `PATH` (ttyd is only needed for browser viewing).
Python attaches its existing TTY; never open a desktop terminal. Screens
keep running across detach until the manager closes. Each screen carries an inherited
`TASKSOLVER_TERMINAL_SCREEN` ownership marker for descendant cleanup; preserve it in
child environments. Provider configuration belongs in TaskSolver, generic screen
lifecycle in wirecap's parent-side runtime. Native capture applies to agy/Codex/Kimi;
the ordinary Claude UI has no wirecap capture. Default captures are temporary; explicit
paths persist. `wirecap.runtime.webterm.WebTerminal` uses ttyd for optional browser
viewing on loopback, with read-only input by default. An explicit `screen=` creates
an independent session containing only that screen's linked window, with tmux navigation
and status hidden; omitting it creates an all-screen grouped viewer. Closing a screen
closes its specific viewers; closing the final screen also closes all-screen viewers.
The manager remains reusable. Use SSH forwarding for remote viewing. The CLI's
`--web-port` serves one page per screen on consecutive ports (0 selects available ports),
also attaches the local TTY when available, and keeps serving after local detach until
Ctrl-C. `--no-attach` with `--web-port` forces web-only operation. Offline checks:
`test_terminal.py`, `test_tasksolver_terminal.py`, `test_webterm.py` and
`test_webterm_views.py`. Run them through Pixi; they send input only to local stubs.

All Claude launches use the bundled bridged runtime. `claude/build_cli.py` verifies
and caches the pristine pin, uses the externally provided `bun_graph` on `PATH`
(patchnar's, with `--natives`; the workflow pins it as `PATCHNAR_REF`),
and installs a split runtime under the SDK's `_bundled/`:
`bun` with `.bun` removed, a native `claude` launcher, `source/`, `assets/`,
`modules/`, and `libwire.node`/`cache.mjs`. The entry disables upstream auto-updates;
update the pin and rebuild instead. There is no raw runtime launch option or PATH
fallback. `pixi run python -m claude.unbundle --output PATH` creates an isolated copy.
Keep the private Bun (`Bun.ant`) and native helper dispatch; `.node` addons move to
`modules/`, while statically linked helpers remain in Bun. Validate native sections
and load mappings independently of the converter, allowing only declared patches.
Use pyelftools' header structures for ELF edits and pwntools for instruction
assembly and binutils discovery; preserve native placement explicitly.
Clear bytecode pointers in the extraction-only copy first: packed caches overlap.
The already-bundled source loader clones files as Latin-1; its one call gets a
five-byte redirection to the compiled UTF-8 shim in `native_cache.c`. Preserve every
other native section and the original RELRO/stack headers. The appended RX section
and redirected call are declared in the manifest. Do not rewrite Unicode source to
ASCII: that changes raw templates. Never hard-code private Bun addresses:
`bun_graph --natives` locates that call and the entry points `libwire.node` calls by
masked instruction signatures that must match once in `.text`; `unbundle.py` checks
the report against the binary and writes `bun_natives.h` (addresses and the code the
addon re-checks). `KNOWN_NATIVES` in `test_claude_unbundle.py` holds hand-verified
reports for known builds. Source patches are content contracts too: anchor them on
Claude's property names and string literals, capture the minified names they need,
require exactly one match across all modules, and never key them on chunk names.

Generate JSC sidecars from the final modified source through `libwire.node`, using
the private runtime's own emitter and proper native reference release. Do not reuse
upstream caches or claim prelinked module metadata is a native addon. The runtime
reconstructs external module records. Verify actual cache hits, Unicode semantics,
relocation and stale-source rejection. Preserve text asset primitive exports with
UTF-8 files and CommonJS adapters; loose Markdown loading changes semantics. Resolve
source imports and worker/browser paths relative to the installed tree.

The headless SDK uses its existing subprocess `stream-json` protocol. Native UI
startup automatically exposes a private Unix socket; managed screens publish it as
`screen.control_socket`. `pyclaude.attach_terminal(screen)` connects the official SDK
to that process. It also accepts a socket path; no SDK launch options are accepted.
Configure the model, workspace and permissions at launch or in the UI. A resumed
session ID starts a separate process; peer inbox messages and `Stop` hooks are not
SDK results. Keep controls independent from Remote Control gates, tee real engine
frames without consuming the engine twice, and route results by input UUID and
engine identity. Native dialogs own permissions. Reject unsupported inputs/controls;
never fabricate results or let a human result finish an SDK query. Detaching a
client leaves its turn running and reserved until completion. Source-only and
transport checks are `test_claude_unbundle.py` and `test_claude_transport.py`;
`test_claude_native.py` drives the real UI against a loopback model fixture,
including permission dialogs, and `node --test test_scripts/test_claude_native_bridge.mjs`
checks the JavaScript bridge lifecycle.

Agy, Codex and Kimi share the linked capture layer; Claude speaks its SDK protocol directly.

- **`wirecap/`**, the shared capture layer:
  - `wirecap/decode/` is **stdlib-import-pure**, because each CLI's embedded interpreter imports it:
    the JSONL `Recorder`, HTTP/1.1+SSE framing, `BaseCorrelator`, HTTP/2 reassembly, the
    `TurnBuilder`/`Usage` contract and `mp_child`. agy's `TurnBuilder` keys responses by
    `responseId` and matches the request model to the served `modelVersion`, because agy streams
    its session-title call alongside the answer. Never import `wirecap.runtime` or `tasksolver`
    here; the `python3 -S` probes in `test_scripts/` enforce it.
  - `wirecap/runtime/` is parent-side: `WirePopen`/`WireProcess`, the PTY flavours, git-workspace
    scoping and the vendored-artifact resolver. Process launch serializes descriptor inheritance
    changes and preserves Python 3.15's resource-tracker fd across `pty.fork`; restore the parent's
    flags before releasing the launch lock. Keep descriptor ownership and child reaping one-shot,
    including failed startup. PTY result/death readiness must drain buffered output before closing.
  - `wirecap/native/` is the C ABI bridge (`libwirecap_bridge.a`) that embeds CPython on a 16 MB-stack
    worker thread, linked into the agy host, the codex binary (from the standalone
    `wirecap/native/build`; `WIRECAP_BRIDGE_DIR` overrides) and the kimi addon. They build with
    GCC LTO (`TASKSOLVER_LTO`, default on); the archive holds fat LTO objects because codex's Rust
    link cannot read GCC bytecode. Repeated starts share one worker; shutdown drains accepted
    events, joins once and prevents restart. Event-kind strings must live for the process lifetime.
    Shared regressions: `test_wire_process.py`, `test_wire_session.py`, `test_wire_bridge.py`,
    and `test_wirecap_framing.py`.
- **`antigravity/`** → the `pyagy` package: Google's Go `agy` converted into `vendor/libagy.so`
  and linked into a C++23 host, `vendor/agy`; the mechanism is in `antigravity/README.md`. Rules:
  - `BOOKS` in `procdef.h` is the one table (`{id, mode, retcap, AGY_FULLGO(symbol)}`, or
    `AGY_ASMGO` / `AGY_OFF`); run `pixi run shim-books` after editing it. Generated
    `symbols_gen.h` holds `AGY_BUILD_ID` and the `AGY_STUBS` list; never edit it.
  - Keep eager PLT binding (lazy resolution would run on the Go stack and clobber R10/R11),
    `FuncFlagAsm` on the synthetic functions, `__builtin_unreachable()` after the stub asm,
    the compile-time `AGY_FRAME <= 128` bound, `goframe.cpp` at `-fno-lto`, and `g_agy_on`
    `used`/`externally_visible` (LTO cannot see the asm's loads). The host exports only what
    libagy.so references; do not add `--export-dynamic`.
  - Synthetic Go frames require precise pointer maps; `FuncFlagAsm` does not change scanning
    into conservative scanning. Generate each saved-register mask and stack-argument map from
    the pinned target's `ArgsPointerMaps[0]` and validated `morestack_noctxt` spills. The locals
    map also marks the initialized `&block` argument. Scalar words and uninitialized register
    spill homes remain unmarked; unsupported register/context layouts fail the symbol build.
  - `symbols/` is a source build package run as `python3 -m symbols.<tool>` from `antigravity/`;
    LIEF writes the ELF layout, pyelftools reads and validates its metadata, Capstone decodes Go
    prologues, spills and the planner-response getter, and pwntools assembles book/PLT jumps with
    the conda toolchain. Keep these build-only
    requirements in `[build-system].requires`, pixi host dependencies and workspace dependencies.
    Source tools stay out of the wheel and embedded runtime's stdlib-only modules. Conversions
    always start from cached raw agy (`build/antigravity-*.raw`, keyed by the archive SHA512).
    `antigravity/vendor.json` pins the archive and the extracted binary (configure verifies both
    and reports a new binary's sha512/size); `shim-symbols` refreshes the outputs, and
    `shim-agy-update` prints the latest archive pin.
  - Run `test_scripts/test_agy_symbols.py` and `test_scripts/test_agy_stubs.py` after build-tool,
    stub or toolchain changes. `test_appresponse.py` checks native planner-response reads,
    Unicode, payload truncation and concurrent stack diagnostics. App/SSE capture errors are
    exposed through `AgyResponse.response_capture_errors`; a truncated app snapshot cannot
    override complete wire text. Request rewriting is unsupported: the TLS book is asynchronous.
- **`codex/`** → the `pycodex` package: OpenAI's Rust codex built from a vendored tree with the bridge
  compiled in. Each transport request owns a `ResponseCapture` ID through SSE/WebSocket events
  and termination; never correlate concurrent streams using a global latest-request ID.
  `test_codex_sdk_native.py` verifies overlapping requests against the real built app-server.
  See `codex/README.md`.
- **`kimi/`** → the `pykimi` package: Moonshot's Node kimi-code built from a vendored tree, with a
  three-line patch loading the bridge as an N-API addon (`kimi/native/`). See `kimi/README.md`.
- **`claude/`** → `pyclaude` and the pristine `claude/vendor/sdk`. `claude/vendor.json` pins the SDK
  tag and a Claude Code release, tracked separately and never older than the one the SDK bundles
  (`build_cli.py` enforces it); `claude/build_cli.py` installs the bridged runtime and
  its external source, caches, assets and addons under the SDK's `_bundled/` before packaging. See `claude/README.md`.

The PTY paths are symmetric (`agy --print` ≡ `codex exec` ≡ `kimi -p`), so a change to one usually
belongs in `wirecap/`. `pyagy.Session`, `pycodex.Session` and `pykimi.Session` share
`wirecap/runtime/session.py` (`ask_turn` and the `WireSession` base); providers supply only process
construction and response shape. Build session processes with `max_wait=None` (asserted in
`_start`), or a deadline kills the bridge mid-session. `pycodex.SDKSession` and `pyclaude.Session`
have their own SDK lifecycles and never use `WireSession`. Failed `WireSession` startup unwinds
all resources, and closing is idempotent; closed sessions and ended workers cannot accept turns.

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
