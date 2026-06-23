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
| vLLM (OpenAI-compatible) | `qwen3`, `qwen3-5`, `qwen3-6` | OpenAI client with a custom `base_url` |
| Moonshot / Kimi | `kimi2-6`, `kimi-k2.7-code` | Anthropic-compatible endpoint |
| Gemini | `gemini-3-pro`, `gemini-3-flash`, `gemini-2.0-flash` | Google GenAI SDK |
| Local HuggingFace | `qwen`, `intern`, `minicpm`, `phi`, `llama` | in-process via `transformers` (needs the `local` extra) |

## Install

TaskSolver uses [pixi](https://pixi.sh) for a managed Python 3.14 environment:

```bash
pixi install                                              # build + install the default env
pixi run python test_scripts/text_only.py --model claude-code
pixi shell                                                # drop into the environment
```

The pixi workspace is configured in `pyproject.toml`: it pins Python 3.14 and builds TaskSolver itself as an **editable** package via the `pixi-build` backend (so source edits are live). The heavy `local` extra is **not** part of the default pixi environment.

Core dependencies are unpinned so the pixi workspace owns version resolution. `flash-attn` is intentionally **not** a dependency — build it yourself if a local model needs it.

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

To dispatch by model-id instead of importing an adapter directly, use `tasksolver.agent.Agent(api_key, task, vision_model="claude-code-sonnet-4-6")` and call `agent.visual_interface.run_once(question)`. Runnable text-only and vision examples live in [`test_scripts/`](test_scripts/).

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
