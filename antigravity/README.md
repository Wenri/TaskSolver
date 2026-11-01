# antigravity — bind custom network / MCP context / tools into `agy`

`antigravity` instruments the **Antigravity CLI** (Google's agentic coding tool, codename *jetski*)
so Python code can observe and modify it in-process, without its source or a plugin API.
`symbols/make_libagy.py` converts the upstream PIE `agy` into `vendor/libagy.so`, patching each
booked Go function with a jump to a host stub; a small C++23 host, `vendor/agy`, links it as its
first `DT_NEEDED`, registers the stubs with Go's runtime and calls `agy_rt0_go`. Book events reach
an embedded CPython interpreter on a worker thread, where your logic lives (`pyagy/agy_process/`).

## Why the design looks like this

| Fact | Consequence |
| --- | --- |
| Go makes syscalls directly, bypassing libc (except the cgo DNS resolver) | `LD_PRELOAD` sees no traffic, so books patch Go functions; `getaddrinfo` interposition can still redirect connections. |
| TLS is BoringCrypto, statically linked | Nothing to preload; the `crypto/tls.(*Conn).Write` book sees plaintext. |
| Stripped, but the pclntab is intact and `pcHeader.textStart == 0` | Names are recoverable, but `debug/gosym` mis-resolves; we locate `moduledata` through the relocation addressing the pclntab and prologue-verify every booked address. |

Everything is pinned to the agy BuildID (currently `85d3425a18a809e545747dcb9d87c03c`, agy
**1.4.11**); the shim installs no books on any other build. After updating the release pins, run
`pixi run shim-symbols`.

## Architecture

```
  Go function ──E9 jmp──▶ .plt.agy ──▶ agy_stub__<id> ──cgocall──▶ C handler (g0 stack)
       ▲                                                              │ copy + enqueue
       └──── resume, or return the handler's verdict ◀────────────────┤ (block if SYNC)
                                                                      ▼
                                 pyworker (16 MB stack, libpython 3.15) → pyagy.agy_process
```

Handlers only copy and enqueue; Python runs on a dedicated thread Go's scheduler never sees.
Books are `ASYNC` (capture) or `SYNC` (block for a verdict and rewrite the buffer in place, same
length or shorter). A blocked `asmcgocall` book stalls the GC, so **SYNC callbacks must be fast**.

`BOOKS` in `src/procdef.h` is the one table (`pyagy.BOOKS` mirrors it). Addresses come from the
generated `symbols/symbols.json`; candidates for new books are in `symbols/funcmap.tsv.gz`. Go's
amd64 register ABI passes arguments in `RAX, RBX, RCX, RDI, RSI, R8, R9, R10, R11` (a `[]byte`
takes three); `R14` holds the goroutine and must never be clobbered.

## The cgocall-trampoline path

Many useful boundaries park the goroutine, which may then resume on another thread, so a book must
not depend on the OS thread or rewrite its return address (frida-gum's inline patching did both and
failed 15–40% of turns). Every book is a static cgocall trampoline that reads entry arguments.

The converter patches each target past its stack check (so `morestack` cannot re-enter the stub)
with a jump to a `.plt.agy` entry whose GOT slot `ld.so` binds eagerly to `agy_stub__<id>`. The stub
loads `g_agy_on[book]`; for a non-null handler it saves the Go argument registers into a stack block
and calls `runtime.cgocall` (`AGY_FULLGO`, GC-safe for functions that may park) or
`runtime.asmcgocall` (`AGY_ASMGO`, hot functions that never park). It then restores the registers
and either returns the handler's verdict or replays the original prologue and resumes through
`agy_resume__<id>`.

Each `procdef.h` row reads `{id, mode, retcap, AGY_FULLGO(symbol)}` (or `AGY_ASMGO` / `AGY_OFF`).
The generated `symbols_gen.h` holds the expected `AGY_BUILD_ID` and `AGY_STUBS`, one
`X("id", go_frame)` per stubbed row. `goframe.cpp` expands it into the naked stub templates, whose
asm a `consteval` builder writes at compile time, and into the `agy_stubs` table of entry and frame
labels. `gomod.cpp` turns that table into the synthetic moduledata Go's GC unwinds the stubs with
(see `gomod.h`); the frame layout is in `goframe.h`.

## Diagnostics

| probe | enable | read |
| --- | --- | --- |
| entry-argument object graph | `AGY_PROC_CGT_ARGS` / `ask(..., arg_probe=True)` | `.cgt_args` |
| deduped Go call stacks | `AGY_PROC_STACK` / `ask(..., stack=True)` | `.stacks`, `.call_graph` |
| backend RPC timeline (`CodeAssistClient.*`) | always on | `.rpc_trace` |
| decoded answer at the app boundary (`updateWithStep`) | always on | `.app_text` |

Stacks need `symbols/funcmap.tsv.gz` (`pixi run shim-symbols`). Offline renderers:
`python3 -m pyagy.agy_process.symbolize <capture.jsonl> [--graph]` and
`python3 -m pyagy.agy_process.rpctrace <capture.jsonl> --stacks`.

## Hybrid tools / mcp_context

agy is an MCP client, so new tools and context come from an MCP server, not from rewriting requests.
`pyagy.config.write_mcp_config(tools, context)` registers `pyagy/agy_mcp_server.py` in
`~/.gemini/config/mcp_config.json`, and `validate_server()` runs the handshake first; `config/mcp.json`
is the manual template.

## Build & run

`pixi install` builds `vendor/libagy.so` and `vendor/agy` (linux-64 only; a missing toolchain fails
the install). CMake caches the pinned raw agy by its SHA512 in `build/`, and every conversion starts
from it; `-DAGY_BIN=/path/to/raw-agy` selects a local copy.

```bash
pixi run shim-agy-update  # print new CMakeLists.txt pins from the latest release manifest
pixi run shim-symbols     # ensure library + metadata are current, then recompile
pixi run shim-books       # after editing src/procdef.h: regenerate pyagy's BOOKS mirror
pixi run build-shim       # rebuild changed outputs
pixi run python test_scripts/test_agy_stubs.py    # after stub or toolchain changes; no model calls
pixi run python test_scripts/test_agy_symbols.py  # parser, resolver, generated sources, conversion
pixi run test_scripts/run-agy.sh --version       # instrumented startup; writes antigravity.log
```

The native code needs conda GCC 15 (constexpr asm strings, C++23). After a compiler-major change,
clear the CMake caches but keep `build/antigravity-*.raw`.

**Rules learned the hard way.** Never book runtime-special functions (`runtime.main`). Patch past
the stack check. Use `AGY_FULLGO` for anything that may park. agy needs a real git workspace (an
empty directory hangs at startup); sign-in works headlessly through its out-of-band OAuth code paste.

## Capturing model traffic

The model endpoint is HTTP/1.1 + SSE (`POST /v1internal:streamGenerateContent?alt=sse`). The
request comes from the `tls_write` book; the response arrives line by line through `resp_chunk`
(`toStreamResponseChunk`) and `usage_delta`, because the decrypted bytes are a return value.
`http1sse.py` decodes them, and `BaseCorrelator.feed_chunk` (`wirecap/decode/`) emits one
`genai_turn` per request. agy's other connections are HTTP/2 (`h2reassemble.py`).

## The `pyagy` client: interact / modify / decode

One call sends a request, optionally modifies the prompt, tools or context, and returns the decoded
answer with everything the run captured.

```python
from pyagy import ask, Session, ToolSpec, ContextResource, RewriteRule

r = ask("Summarize this repo.")                 # one turn; installs the full book union
print(r.text)                                    # clean final answer (prefers app→wire→transcript)
print(r.model, r.usage.total_tokens)             # decoded from the model turn
print(r.request["tools"], r.request["first_user_text"])   # the request as SENT
print(len(r.turns), [t["text"][:20] for t in r.turns])    # every wire model turn decoded
print(r.app_text)                                # answer decoded at agy's consumer boundary
print(r.rpc_trace)                               # labeled backend-RPC timeline

with Session(tools=[ToolSpec("weather", handler="mytools:weather")]) as s:
    print(s.ask("Weather in Paris?").text)       # agy can call the injected tool
    s.set_rewrite([RewriteRule("Paris", "Tokyo")])  # live, equal-length substitution
    print(s.ask("And there?").text)
```

Every run installs all books, so one turn fills every surface below; a field is empty or `None`
when the run captured nothing of that kind. `.text` prefers the app boundary, then the wire, then
the PTY transcript, and reports which it used in `.source`.

| surface | books | `AgyResponse` field |
|---|---|---|
| wire *(the `rewrite=` surface)* | egress `tls_write` + `resp_chunk` | `.turns` / `.request` / `.events` / `.usage` / `.model` |
| app boundary | framework consumer (`updateWithStep`) | `.app_text` (+ `.source=="app"`) |
| rpc trace | `CodeAssistClient.*` backend RPCs | `.rpc_trace` |
| overlay `stack=True` | Go call-stack unwind at each fire | `.stacks` (rendered) / `.call_graph` (`Counter`) |
| overlay `arg_probe=True` | arg-graph walk at entry | `.cgt_args` (list of reports) |

- **Two composable ways to modify:** `rewrite=` (a `RewriteRule` list, a `"module:func"` string or
  a callable) makes `tls_write` a live SYNC egress rewrite, equal-length only (a length-changing
  rule is skipped and recorded as `rewrite_skip`); `tools=` / `context=` add an MCP server.
- **Introspection:** `from pyagy import BOOKS, by_mech, enabled_books, by_kind, sync_capable,
  DERIVED_KINDS`, the machine-readable mirror of `src/procdef.h`.
- **Env knobs** (the kwargs set them; diagnostics are in the table above):

  | env var | set by | effect |
  |---|---|---|
  | `AGY_PROC_ENABLE` | (auto) | the one gate: install all books |
  | `AGY_PROC_TLS_WRITE_SYNC` | `rewrite=` | make `tls_write` a SYNC (modify) book |
  | `AGY_PROC_REWRITE_RULES` / `AGY_PROC_REWRITE` | `rewrite=` | rules-file path / `module:func` |
  | `AGY_PROC_CONV_ID` | (auto) | enable the `file_open` book → `conversation_id` event |
  | `AGY_PROC_H2` / `WIRE_CORRELATE` | — | `=0` disables HTTP/2 reassembly / the turn correlator (shared `WIRE_*` names; legacy `AGY_PROC_*` accepted) |

- **Always instrumented:** every launch goes through `AgyProcess` on the pinned `vendor/agy`. The
  shim loads through agy's own `PT_INTERP` with `--preload` (`_env.preload_argv`), not
  `LD_PRELOAD`, so the processes agy spawns never inherit it; `--library-path` resolves libpython.
- **Tests:** offline `test_http1sse.py`, `test_config.py`, `test_client.py`,
  `test_appresponse.py`, `test_rpctrace.py`, `test_symbolize.py`; live (skipping cleanly)
  `test_rewrite.py`, `test_trampoline.py`, `test_agyprocess.py`, `test_agy_session.py`.

## Sessions

agy persists each conversation under `~/.gemini/antigravity-cli/` and resumes it on a new launch.
`Session` is the first-class object (on `wirecap.runtime.session.WireSession`); `ask()` is one-shot
sugar. Both carry a `conversation_id` that `pyagy.resume(cid)`, `pyagy.continue_latest()`,
`pyagy.list_conversations()` and `pyagy.read_transcript(cid)` work with, as do
`AgyProcess(conversation_id=)` and `AgyModel(multi_turn=True)`.

- **Ids** come from the `file_open` book, which reads the uuid out of the store path agy opens;
  without that event the newest `*.db` by mtime wins.
- **Workspace trust** is on by default: agy refuses to start interactively in an untrusted
  workspace and has no flag for it, so each launch adds its workspace to `settings.json →
  trustedWorkspaces`. `trust=False` opts out, `skip_permissions=True` uses the blunt flag.
- **`data_dir=`** scopes the store to a project. agy hardcodes `$HOME/.gemini`, so this overrides
  HOME and links the real login token in, leaving the global store untouched. Git-ignore the
  scoped `.gemini/`.

## `AgyProcess`: the single agy launcher

Every launch goes through `AgyProcess`, a spawn `Process` on the pinned `vendor/agy`. As with
`multiprocessing.Process`, the **caller** owns the result `SimpleQueue` and passes it in `args`;
the target then runs *inside* agy's embedded interpreter and puts native Python objects home. The
default target, `stream_turns`, streams the decoded answer, which is what `client.py` drains.

```python
q = get_context("spawn").SimpleQueue()
p = AgyProcess(target=stream_turns, args=(q,), prompt="What is 2+2?")
p.start(); q._writer.close()           # the reader EOFs if agy dies
while p.service_pty(1.0, [q._reader]): # drain the PTY in the same wait that reads the queue
    o = q.get()                         # {"kind": "genai_turn", …} or ("_agy_done", code)
    if isinstance(o, tuple): break
```

The queue's POSIX semaphores need a working `sem_open`. Tests: `test_scripts/test_agyprocess.py`.

## Using agy as a TaskSolver backend (`pyagy`)

`AgyModel` gives agy TaskSolver's adapter surface (like `ClaudeCodeModel`) over `pyagy.ask`: it
runs `agy --print` in a throwaway git workspace and parses the reply. No API key; agy uses its own
login in `~/.gemini/antigravity-cli/`.

```python
from pyagy import AgyModel
model = AgyModel(api_key=None, task=my_task, model="gemini-3-pro")
parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))
```

Images reach agy as PNG files (inside the workspace when one is set), named by absolute path; the
prompt tells the agent to open them with `view_file`. Smoke test:
`pixi run python test_scripts/example_agy_backend.py`. Use `pyagy.Session` for multi-turn work.

### Chat mode (`AgyModel(chat=True)`)

To use agy like a chat-completion call (e.g. as a judge on a subscription login), `chat=True`
writes `<workspace>/.agents/agents/tasksolver-chat/agent.md` and runs each call with
`--agent tasksolver-chat --effort low`:

```yaml
tools:
  - view_file                  # the only tool: opens the image files
excludeDefaultComponents: true # no default prompt sections or built-in tools
inheritCustomizations: false   # none of the user's skills, rules, plugins, subagents, MCP servers
mainAgent: true
subagent: false
model: inherit                 # the --model flag decides
commandExecutionPolicy: off
```

Needs agy >= 1.2.11 (earlier releases ignore workspace agents under `--print`). Using any other
tool, or opening a file it was not handed, raises `ChatModeViolation`; the harness still offers
`manage_task`, and agy still makes its own small session-title call.

## Risks / limitations

- **Locked-down containers:** configure downloads the pinned agy and the pixi installer uses
  GitHub releases; point `-DAGY_BIN=` at a local raw agy if egress is blocked.
- **WSL1 hosts:** the setuptools link step needs the `wsl1-exec.so` shim in `LD_PRELOAD`.
- This instruments a binary **you run on your own machine** for research and interop; it does
  not cross anyone else's security boundary.
