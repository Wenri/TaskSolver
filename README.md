# TaskSolver

A small, **provider-agnostic query flow for vision-language models**. You define a `TaskSpec` (a prompt plus the answer type that parses the model's reply), pick a backend with a model-id string, and call it — TaskSolver routes the request to the right provider and hands back a parsed answer with retry-on-parse-failure built in.

Originally built for [BlenderAlchemy](https://github.com/ianhuang0630/BlenderAlchemyOfficial)-style VLM systems; used by [3D-CoT](https://github.com/Wenri/3D-CoT).

## Backends

One `Agent`, many backends — selected by the `vision_model` id. Provider adapters are imported **lazily**, so you only need the SDK for the backend you actually use.

| Backend | Example model-ids | Transport |
| --- | --- | --- |
| OpenAI | `gpt-4o`, `gpt-4o-mini`, `o1`, `o3-mini` | OpenAI API (`chat.completions`) |
| Anthropic | `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5` | Anthropic Messages API |
| Claude Code CLI | `claude-code-sonnet-4-6`, `claude-code-opus-4-7`, `claude-code-fable-5` | local `claude` CLI subprocess |
| Antigravity CLI | `agy`, `antigravity`, `agy-gemini-3-pro` | local `agy` CLI under a PTY (`pyagy`); wheel installs set `AGY_BIN`+`AGY_SHIM` |
| Codex CLI | `codex`, `codex-gpt-5-codex` | local `codex exec` subprocess (`pycodex`); wheel installs set `CODEX_BIN` |
| Kimi Code CLI | `kimi-code`, `kimi-code-k3` | vendored Node CLI under a PTY (`pykimi`), wirecap bridge via an N-API addon; built from source by `pixi install` |
| vLLM (OpenAI-compatible) | `qwen3`, `qwen3-5`, `qwen3-6` | OpenAI client with a custom `base_url` |
| Moonshot / Kimi (HTTP) | `kimi2-6`, `kimi-k2.7-code` | Anthropic-compatible endpoint (the HTTP `KimiModel`, distinct from the `kimi-code` CLI) |
| Gemini | `gemini-3-pro`, `gemini-3-flash`, `gemini-2.0-flash` | Google GenAI SDK |
| Local HuggingFace | `qwen`, `intern`, `minicpm`, `phi`, `llama` | in-process via `transformers` (needs the `local` extra) |

The agy/codex/kimi-code native artifacts embed CPython (the shim's interpreter, codex's libpython, the `wirecap_node` addon's libpython): a consuming environment must run the **same Python minor version** as the env they were built in (this repo pins 3.13) — a version-mismatched consumer is an unsupported configuration.

## Install

TaskSolver uses [pixi](https://pixi.sh) for a managed Python 3.13 environment:

```bash
pixi install                                              # build + install the default env
pixi run python test_scripts/text_only.py --model claude-code
pixi shell                                                # drop into the environment
```

The pixi workspace is configured in `pyproject.toml`: it pins Python 3.13 and builds TaskSolver itself as an **editable** package via the `pixi-build` backend (so source edits are live). It defines **two** environments: the **default** env (`pixi install`) carries the core API / Claude Code / Gemini backends plus the UI/plotting tools and builds the `antigravity` shim — **no GPU required**; the **`cuda`** env (`pixi install -e cuda`) adds the local HuggingFace + torch (cu130) adapters and **flash-attn**, so it needs a CUDA 13 GPU and builds flash-attn from source on first run (uv caches the wheel afterward).

For à-la-carte use, the same groups are portable `[project.optional-dependencies]` extras — `tasksolver[local]` (torch + HuggingFace adapters, **including flash-attn**) and `tasksolver[app]` (UI/plotting) — so the package stays installable as a dependency by uv and pixi (a consumer points at its own torch index). Core dependencies are unpinned so the consuming workspace owns version resolution; `flash-attn` has no prebuilt wheels for new Pythons and builds from source, so `tasksolver[local]` needs CUDA.

Credentials are supplied via a `KeyChain` (loading files like `system/credentials/openai_api.txt`) or environment variables (`OPENAI_API_KEY`, the `claude` key, `GEMINI_API_KEY`, `VLLM_API_KEY`, `MOONSHOT_API_KEY`).

## Usage

Define an answer type (parses the raw reply; raise `GPTOutputParseException` to trigger a retry), wrap it in a `TaskSpec`, then query a backend:

```python
from tasksolver.common import TaskSpec, ParsedAnswer, Question, KeyChain
from tasksolver.exceptions import GPTOutputParseException

class HeadsOrTails(ParsedAnswer):
    def __init__(self, value): self.value = value
    @staticmethod
    def parser(raw: str) -> "HeadsOrTails":
        out = raw.strip().strip(".,").lower()
        if out not in ("heads", "tails"):
            raise GPTOutputParseException("expected `heads` or `tails`")
        return HeadsOrTails(out)
    def __str__(self): return self.value

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
spec normalization and the per-CLI config serializations (claude
`--mcp-config`/`--strict-mcp-config`, codex `-c mcp_servers.<name>=<TOML>`,
kimi-code's discovered `mcpServers` JSON, qwen config fragments, the shared
`mcpServers` JSON writer), so harnesses that drive the CLIs directly can render
the exact same configs.

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

[`antigravity/`](antigravity/) is a research subsystem that instruments Google's Antigravity CLI (`agy`) in-process via an `LD_PRELOAD` shim (frida-gum inline hooks + an embedded CPython), and also exposes `agy` as a TaskSolver-style backend (`pyagy.AgyModel`, mirroring `ClaudeCodeModel`). See [`antigravity/README.md`](antigravity/README.md) for the design, the cgocall-trampoline hook mechanism for parking Go functions, and build/validation notes — validated on both WSL1 and a real cloud kernel (6.18.5, agy 1.0.15), including a gdb instruction-level root-cause proof.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
