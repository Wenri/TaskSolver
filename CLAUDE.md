# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TaskSolver is a **provider-agnostic query flow for vision-language models**. You define a `TaskSpec` (prompt + an answer type that parses the model's reply), select a backend by a model-id string, and call it; TaskSolver routes to the right provider adapter and returns a parsed answer with retry-on-parse-failure built in. Originally built for BlenderAlchemy-style VLM systems; consumed by external projects (3D-CoT, BlenderGym), so model-id aliases are part of the public contract — renaming them is a breaking change.

## Commands

```bash
# pixi (managed env; pins Python 3.14, builds tasksolver via the pixi-build backend)
pixi install                                           # build + install the default env
pixi run python test_scripts/text_only.py --model claude-code
pixi shell                                             # interactive shell inside the env

# Smoke tests (these ARE the test suite — no pytest, no lint config)
python test_scripts/text_only.py --model claude-code   # choices: claude, claude-code, gpt, gemini, qwen, intern
python test_scripts/vision_language.py                 # vision; currently hardcoded to QwenModel (needs [local])
```

There is no formal test/lint/CI setup. The `test_scripts/` files are runnable smoke tests, not automated tests — verify changes by running the relevant one with a backend you have credentials for. `requires-python` is ≥3.10, but the pixi env pins **3.14** (the reason `pymongo` is used instead of a standalone `bson`, which breaks on 3.14). Core deps are intentionally **unpinned** (the host env owns version resolution); `flash-attn` is deliberately not a dependency.

**pixi-build env (`[tool.pixi.*]` in `pyproject.toml`):** the workspace uses the `pixi-build` preview feature — `tasksolver` is built as a noarch conda package by the `pixi-build-python` backend and depended on by path, so `pixi install` installs it **editable** automatically (no separate `pip install -e` needed). Two non-obvious bits if you edit this config: the backend builds *without isolation*, so the `[build-system].requires` (setuptools/pip) must be repeated under `[tool.pixi.package.host-dependencies]`; and the backend resolves from pixi's default channel and is not pinned in `pixi.lock`, so no explicit `channels` are required. The heavy `local` extra is excluded from the default pixi environment (its ML stack may lag a new Python release).

## Architecture

### One Agent, many backends — lazy string dispatch
`tasksolver/agent.py` `Agent.__init__` is a big `if/elif` over the `vision_model` string. The matching branch **lazily imports** that provider's adapter (so you only need the SDK for the backend you use) and stores an instance on `self.visual_interface`. To dispatch by id, construct `Agent(api_key, task, vision_model=...)` and call `agent.visual_interface.run_once(question)`. Model-id → backend map (with alias normalization) lives entirely in this method; e.g. `claude-code-sonnet-4-6` → CLI `claude-sonnet-4-6`, `gemini-3-pro` → `gemini-3-pro-preview`.

### The backend adapter contract (duck-typed — no shared base class)
Every adapter (`GPTModel`, `ClaudeModel`, `ClaudeCodeModel`, `VLLMModel`, `KimiModel`, `GeminiModel`, and the local HF ones `QwenModel`/`InternModel`/`MiniCPMModel`/`PhiModel`/`LlamaModel`) is a standalone class that independently implements the same surface. There is no ABC enforcing it — match the existing shape exactly when adding one:

- `__init__(api_key, task, model=...)`
- `prepare_payload(question, max_tokens, ...)` *(staticmethod)* → provider-specific request dict
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
Every `rough_guess`/`many_rough_guesses` runs: `ask` → `answer_type.parser(content)` → on `GPTOutputParseException`, retry up to `max_tries` (**default 1**), else raise `GPTMaxTriesExceededException`. Parsing — not the HTTP call — is what drives retries.

### Response metadata attachment
`attach_response_metadata` (common.py) decorates the parsed answer with `.llm_response_metadata`, `.request_payload`, and an extracted `.explicit_reasoning_output` — `extract_explicit_reasoning_output` digs reasoning/thinking traces out of varied provider response shapes (Anthropic thinking blocks, vLLM `reasoning_content`, etc.). Preserve this when touching adapters; downstream consumers read these attributes.

### Credentials — `KeyChain` and the service-name gotcha
`KeyChain.add_key(service, key)` stores `key` literally, or reads the first line if `key` is an existing file path. **Gotcha:** when you pass a `KeyChain` into `Agent`, the service names it looks up are `openai`, `claude`, `gemini`, `vllm`, `moonshot` — NOT the `*_api_key` names used in the README/test-script examples. Those examples work only because they index the KeyChain to a *string* (`api_dict['claude_api_key']`) and pass it straight to an adapter, bypassing `Agent`'s lookup. If you wire a KeyChain through `Agent`, name the services `openai`/`claude`/`gemini`/`vllm`/`moonshot`.

Env-var fallbacks resolved inside the adapters (see `vllm.py`, `kimi.py`): vLLM uses `VLLM_API_KEY` + a base URL from `QWEN3_OPENAI_BASE_URL`/`QWEN3_BASE_URL`/`VLLM_OPENAI_BASE_URL`/`VLLM_BASE_URL` (unless a builtin endpoint like `qwen3-5`/`qwen3-6` is selected, which hardcodes both); Kimi uses `MOONSHOT_API_KEY` against the fixed `https://api.kimi.com/coding`.

### Claude Code CLI backend is a subprocess, not an SDK
`claude_code.py` shells out to the local `claude` binary (`claude -p <prompt> --output-format json --tools Read --permission-mode acceptEdits`), so it needs the CLI installed (`npm install -g @anthropic-ai/claude-code`) and logged in (`claude /login`) — no API key. Vision inputs are written to local files and the prompt instructs the CLI to `Read` them. `n_choices > 1` runs concurrent CLI threads.

### TAORI agent loop (scaffolding — mostly unused today)
`Agent` also exposes a higher-level **think / act / observe / reflect / interject** loop backed by an `EventCollection` of typed `Event`s (`event.py`: `ThinkEvent`, `ActEvent`, `EvaluateEvent`, …). `act`, `observe`, and `run` are `@abstractmethod` — intended to be subclassed per environment/task. Current real usage drives `visual_interface.run_once()` / `rough_guess()` directly and does not exercise this loop; treat it as an extension point, not load-bearing code.

## Adding a new backend

1. Create `tasksolver/<name>.py` with a class implementing the adapter contract above (copy the closest existing adapter — `gpt4v.py` for OpenAI-compatible, `claude.py` for Anthropic-style — and keep the 4-tuple return + retry loop + `attach_response_metadata`).
2. Add an `elif vision_model in (...)` branch to `Agent.__init__` with a **lazy** `from .<name> import <Class>` inside the branch. The `# TODO: Add your own model here` comments mark the spot.
3. If it takes a credential, decide its `KeyChain` service name and/or env-var fallback and follow the resolver pattern in `vllm.py`/`kimi.py`.
