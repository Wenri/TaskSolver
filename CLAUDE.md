# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TaskSolver is a **provider-agnostic query flow for vision-language models**. You define a `TaskSpec` (prompt + an answer type that parses the model's reply), select a backend by a model-id string, and call it; TaskSolver routes to the right provider adapter and returns a parsed answer with retry-on-parse-failure built in. Originally built for BlenderAlchemy-style VLM systems; consumed by external projects (3D-CoT, BlenderGym), so model-id aliases are part of the public contract — renaming them is a breaking change.

## Commands

```bash
# pixi: pins Python 3.13, builds tasksolver editable via the pixi-build backend.
# TWO envs (pyproject [tool.pixi.environments]): `default` = core + app (UI/plotting +
# the antigravity shim build) — lightweight, NO CUDA/torch, installs GPU-free; `cuda`
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

# Smoke tests (these ARE the test suite — no pytest, no lint config)
python test_scripts/text_only.py --model claude-code   # choices: claude, claude-code, gpt, gemini, qwen, intern
pixi run -e cuda python test_scripts/vision_language.py # vision; uses QwenModel (torch → needs the `cuda` env)
```

There is no formal test/lint/CI setup. The `test_scripts/` files are runnable smoke tests, not automated tests — verify changes by running the relevant one with a backend you have credentials for. `requires-python` is ≥3.10, but the pixi env pins **3.13** (the reason `pymongo` is used instead of a standalone `bson`, which breaks on 3.14). Core deps are intentionally **unpinned** (the host env owns version resolution).

**pixi-build env (`[tool.pixi.*]` in `pyproject.toml`):** the workspace uses the `pixi-build` preview feature — `tasksolver` is built as a noarch conda package by the `pixi-build-python` backend and depended on by path, so `pixi install` installs it **editable** automatically (no separate `pip install -e` needed). Two non-obvious bits if you edit this config: the backend builds *without isolation*, so the `[build-system].requires` (setuptools/pip) must be repeated under `[tool.pixi.package.host-dependencies]`; and the backend resolves from pixi's default channel and is not pinned in `pixi.lock`, so no explicit `channels` are required.

**Optional extras, the merged env, and building flash-attn.** `[project.optional-dependencies]` keeps two *portable* extras — `local` (torch/HF stack **incl. flash-attn**, since the intern/phi/minicpm/llama adapters hardcode `flash_attention_2`) and `app` (streamlit/flask/matplotlib). TaskSolver's workspace maps them to **two** pixi environments (`[tool.pixi.environments]`): `default` = `app` only (GPU-free — what plain `pixi install` resolves; verified to install without a GPU), and `cuda` = `local` + `app`. The cu130 torch index, `no-build-isolation`, and the flash-attn build toolchain live in `[tool.pixi.feature.local.*]`; the CUDA 13 requirement is a virtual package on the workspace `platforms` but **only binds the `cuda` env**. So `pixi install -e cuda` (or `pip install tasksolver[local]`) requires CUDA 13 and compiles flash-attn; plain `pixi install` does not. That build has two non-obvious knobs, set in `[tool.pixi.feature.local.activation.env]` (pixi *does* apply a feature's `activation.env` during the build): use **`FLASH_ATTN_CUDA_ARCHS`** (e.g. `"80"` = Ampere) — flash-attn silently ignores `TORCH_CUDA_ARCH_LIST` and otherwise builds a 4-arch fat binary (~4× the work); and set **`MAX_JOBS`** explicitly, else flash-attn's `psutil`-based auto-calc under-parallelizes because this host's ZFS ARC cache deflates "available" memory.

## Architecture

### One Agent, many backends — lazy string dispatch
`tasksolver/agent.py` `Agent.__init__` is a big `if/elif` over the `vision_model` string. The matching branch **lazily imports** that provider's adapter (so you only need the SDK for the backend you use) and stores an instance on `self.visual_interface`. To dispatch by id, construct `Agent(api_key, task, vision_model=...)` and call `agent.visual_interface.run_once(question)`. Model-id → backend map (with alias normalization) lives entirely in this method; e.g. `claude-code-sonnet-4-6` → CLI `claude-sonnet-4-6`, `gemini-3-pro` → `gemini-3-pro-preview`.

### The backend adapter contract (duck-typed, with one shared base for the CLI backends)
The HTTP/SDK adapters (`GPTModel`, `ClaudeModel`, `VLLMModel`, `KimiModel`, `GeminiModel`, and the local HF ones `QwenModel`/`InternModel`/`MiniCPMModel`/`PhiModel`/`LlamaModel`) are standalone classes that independently implement the same surface. There is no ABC enforcing it — match the existing shape exactly when adding one.

The four **CLI-subprocess** backends are the exception: `ClaudeCodeModel`, `pyagy.AgyModel`, `pycodex.CodexModel` and `pykimi.KimiCodeModel` all subclass `CLIBackendModel` (`tasksolver/cli_backend.py`), which owns `prepare_payload`, the retry loop in `rough_guess`/`many_rough_guesses` (including the `GPTMaxTriesExceededException` context), `run_once`, and a default `ask`/`_finish` for the wirecap clients. `ask` is the one genuinely per-CLI method, so it is overridable, not forced — `ClaudeCodeModel` replaces it entirely (threaded `claude -p` subprocesses, raw CLI JSON as metadata) while agy, codex and kimi-code inherit it and supply only `_client_ask_many` + `_call_kwargs`. Adding another CLI backend = subclass it and set `backend_label` / `command_label` / `generic_model_aliases` / `vision_preamble` / `no_output_hint` / `_client_ask_many`. Adding an HTTP/SDK backend still means copying the closest existing adapter — do **not** retrofit those onto this base; they share no subprocess/workspace machinery. `CLIBackendModel` lives in `tasksolver/` (never `wirecap/`, whose stdlib-import purity is enforced by the `python3 -S` probes in `test_scripts/`) and must be imported only from a provider's `model.py`, which the `pyagy`/`pycodex`/`pykimi` PEP-562 lazy `__getattr__` keeps out of the CLIs' embedded interpreters.

The three instrumented CLIs share `wirecap` but differ in how the bridge is hosted: agy LD-preloads a C++ shim into a closed Go binary; codex compiles the bridge into a from-source Rust binary; **kimi-code** (`kimi/`) is a from-source Node CLI that loads the bridge as an N-API addon (`kimi/native/wirecap_node.node`) via a three-line source patch in the vendored tree (`packages/agent-core-v2/src/wiretap/wiretap.ts` + two emit sites). All three then run identically: `WirePtyPopen` + `mp_child` streaming decoded turns home, capture JSONL authoritative. The kimi-only wrinkle is that node `dlopen`s the addon `RTLD_LOCAL`, so the addon promotes libpython to the global namespace (`dlopen(RTLD_GLOBAL|RTLD_NOLOAD)`) before `wire_start()` or the interpreter's stdlib C extensions can't resolve the Python C-API.

- `__init__(api_key, task, model=...)`
- `prepare_payload(question, max_tokens, ...)` *(a staticmethod on the HTTP adapters; a **classmethod** on `CLIBackendModel`, so the vision preamble and error text follow the subclass — callable either on the class or an instance)* → provider-specific request dict
- `ask(payload, n_choices=1)` → `(messages, metadata)`
- `rough_guess(question, max_tokens, max_tries=1, ...)` → the **4-tuple** below, wrapping the retry loop
- `run_once(question, max_tokens)` → calls `self.task.first_question(question)` then `rough_guess`
- `many_rough_guesses(num_threads, question, ...)` → parallel sampling → list of 4-tuples

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

### Claude Code CLI backend is a subprocess, not an SDK
`claude_code.py` shells out to the local `claude` binary (`claude -p <prompt> --output-format json --tools Read --permission-mode acceptEdits`), so it needs the CLI installed (`npm install -g @anthropic-ai/claude-code`) and logged in (`claude /login`) — no API key. Vision inputs are written to local files and the prompt instructs the CLI to `Read` them. `n_choices > 1` runs concurrent CLI threads.

### TAORI agent loop (scaffolding — mostly unused today)
`Agent` also exposes a higher-level **think / act / observe / reflect / interject** loop backed by an `EventCollection` of typed `Event`s (`event.py`: `ThinkEvent`, `ActEvent`, `EvaluateEvent`, …). `act`, `observe`, and `run` are `@abstractmethod` — intended to be subclassed per environment/task. Current real usage drives `visual_interface.run_once()` / `rough_guess()` directly and does not exercise this loop; treat it as an extension point, not load-bearing code.

### MCP provisioning at session start (`wirecap/runtime/mcp.py`)

The canonical MCP server spec is a plain dict `{"command", "args", "env"}`; `wirecap/runtime/mcp.py`
owns normalization (`normalize_server_spec`, the idempotent `env_wrapped`) and the per-CLI
serializations (`codex_config_flags` — bare-TOML-key names enforced, `claude_mcp_args`,
`qwen_mcp_servers`, `kimi_mcp_json`/`kimi_mcp_toml_lines`, `write_mcp_servers_json` with merge
semantics). It is parent-side runtime code (never imported from `wirecap.decode`). The backends
thread `mcp_servers=` through: `Agent(mcp_servers=, workspace=)` → `ClaudeCodeModel` (config file +
`--mcp-config --strict-mcp-config`, workspace honored as cwd), `AgyModel`/`pyagy.ask/Session`
(`pyagy.write_mcp_servers` into the scoped or global store; `prepare_scoped_home(...,
link_global_config=False)` + `seed_onboarding` make a scoped home self-contained and
already-onboarded; commands get the `/usr/bin/env -u PYTHONHOME` wrap so agy-spawned servers can
start their own interpreters), `CodexModel`/`pycodex.ask/Session` (rendered `-c` flags via
`pycodex.mcp_flags` — same PYTHONHOME unwrap as pyagy, for the same reason), and
`KimiCodeModel`/`pykimi.ask` (kimi-code has no MCP flag — `pykimi.config.mcp_json` writes the
`mcpServers` document to the workspace-local `.kimi-code/mcp.json` the CLI auto-discovers, same
PYTHONHOME unwrap). Non-CLI backends raise on `mcp_servers`/`workspace`. Offline tests:
`test_scripts/test_mcp_serializers.py`, `test_scripts/test_kimi_argv.py`.

pycodex additionally carries the harness-shaped one-shot controls on `ask()` — `codex_home=`
(store scoping, honored by the session readers too), `session_id=`/`continue_latest=`
(non-interactive `codex exec resume`; the returned `CodexResponse.session_id` is store-read so a
silently-forked new thread is visible), `prompt_via_stdin=` (fd 0 = an unlinked temp file,
`codex exec -`), `capture=` (path override), and `CodexResponse.timed_out`. `WirePopen.close` is
group-aware for BOTH providers: leader SIGTERM → bounded reap → `killpg` sweep (PTY children are
session leaders, so the sweep hits exactly the CLI's own MCP/tool children — on success paths
too), and pidfd-sentinel popens close their PTY master via `_teardown_fds`. Offline tests:
`test_scripts/test_codex_argv.py` (argv/env/sessions/mcp_flags) and
`test_scripts/test_codex_process.py` (stub-binary end-to-end: stdin round-trip, timeout, group
sweep, fd stability).

## Subsystems: the two instrumented-CLI backends

Three of the backends are whole subsystems in their own package roots, not single adapter files,
and `tasksolver/agent.py` dispatches into all three. They share a layer:

- **`wirecap/`** — the shared capture layer, consumed by BOTH.
  - `wirecap/decode/` is **stdlib-import-pure** (it is imported by the CPython interpreter embedded
    inside the instrumented CLI): the JSONL `Recorder`, HTTP/1.1+SSE framing, `BaseCorrelator`,
    HTTP/2 reassembly, the `TurnBuilder`/`Usage` contract, and `mp_child` (the in-host
    multiprocessing child). Never import `wirecap.runtime` or `tasksolver` from here — the
    `python3 -S` probes in `test_scripts/` (one per instrumented CLI's dispatch module, plus the
    decode-layer probe) enforce it, and they are the tripwire for any move.
  - `wirecap/runtime/` is parent-side: `WirePopen`/`WireProcess`, the PTY flavours
    `WirePtyPopen`/`WirePtyProcess`, git-workspace scoping, the vendored-artifact resolver.
  - `wirecap/native/` is the C ABI bridge (`libwirecap_bridge.a`) that embeds CPython on a
    16 MB-stack worker thread; linked into the agy shim, the codex binary, and the kimi-code addon.
- **`antigravity/`** → the `pyagy` package: Google's Go `agy` CLI, instrumented by an LD_PRELOAD
  C++23 shim (`antigravity/src/`) that patches cgocall trampolines over recovered Go addresses.
  See `antigravity/README.md`.
- **`codex/`** → the `pycodex` package: OpenAI's Rust codex, built from a vendored source tree with
  the bridge compiled in (no preload needed). See `codex/README.md`.
- **`kimi/`** → the `pykimi` package: Moonshot's Node kimi-code, built from a vendored source tree
  with a three-line wiretap patch that loads the bridge as an N-API addon (`kimi/native/`). See
  `kimi/README.md`.

The three are kept deliberately symmetric — `agy --print` ≡ `codex exec` ≡ `kimi -p`, and all run
under the same PTY machinery and stream turns home over the same mp-child channel — so a change to
one usually belongs in `wirecap/` rather than duplicated.

Note the test surface is wider than the two smoke scripts named under Commands: `test_scripts/`
holds ~12 offline tests (decode, correlator, config injection, client accessors, the purity probes)
plus the live agy/codex ones. They are plain `python3 test_scripts/<f>.py`; there is still no pytest.

## Adding a new backend

1. Create `tasksolver/<name>.py` with a class implementing the adapter contract above (copy the closest existing adapter — `gpt4v.py` for OpenAI-compatible, `claude.py` for Anthropic-style — and keep the 4-tuple return + retry loop + `attach_response_metadata`). For a **CLI-subprocess** backend, subclass `tasksolver/cli_backend.py`'s `CLIBackendModel` instead of copying an adapter — it already supplies the payload assembly, retry loop and 4-tuple.
2. Add an `elif vision_model in (...)` branch to `Agent.__init__` with a **lazy** `from .<name> import <Class>` inside the branch. The `# TODO: Add your own model here` comments mark the spot.
3. If it takes a credential, decide its `KeyChain` service name and/or env-var fallback and follow the resolver pattern in `vllm.py`/`kimi.py`.
