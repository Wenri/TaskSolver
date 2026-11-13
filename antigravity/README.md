# antigravity — capture `agy` traffic and supply tools / context

`antigravity` instruments the **Antigravity CLI** (Google's agentic coding tool, codename *jetski*)
to capture model traffic and native agent events without its source or a plugin API. Python
clients supply additional tools, context and system notifications through the CLI's supported
interfaces.
`symbols/make_libagy.py` converts the upstream PIE `agy` into `vendor/libagy.so`, patching each
booked Go function with a jump to a host stub; a small C++23 host, `vendor/agy`, links it as its
first `DT_NEEDED`, registers the stubs with Go's runtime and calls `agy_rt0_go`. Book events reach
an embedded CPython interpreter on a worker thread, where your logic lives (`pyagy/agy_process/`).

To use the native interactive UI, run `pixi run python -m tasksolver.terminal agy`.
Python manages named screens through the shared wirecap terminal manager, with optional
browser viewing and the same linked capture. See [managed terminal screens](../README.md#managed-terminal-screens).

## Binary constraints

| Fact | Consequence |
| --- | --- |
| Go makes syscalls directly, bypassing libc (except the cgo DNS resolver) | `LD_PRELOAD` sees no traffic, so books patch Go functions; `getaddrinfo` interposition can still redirect connections. |
| TLS is BoringCrypto, statically linked | Nothing to preload; the `crypto/tls.(*Conn).Write` book sees plaintext. |
| Stripped, but the pclntab is intact and `pcHeader.textStart == 0` | Names are recoverable, but `debug/gosym` mis-resolves; we locate `moduledata` through the relocation addressing the pclntab and prologue-verify every booked address. |

Everything is pinned to the BuildID of the agy release in [`vendor.json`](vendor.json)
(`symbols/symbols.json` records it at build time); the shim installs no books on any other build.
`python3 tools/runtimes.py adopt agy` pins the latest release; then run `pixi run shim-symbols`.

## Architecture

```
  Go function ──E9 jmp──▶ .plt.agy ──▶ agy_stub__<id> ──cgocall──▶ C handler (g0 stack)
       ▲                                                              │ copy + enqueue
       └──── resume, or return the handler's verdict ◀────────────────┤ (block if SYNC)
                                                                      ▼
                                 pyworker (16 MB stack, libpython 3.15) → pyagy.agy_process
```

Capture handlers copy and enqueue; Python runs on a dedicated thread Go's scheduler never sees.
Capture books are `ASYNC`; the `SYNC` exit book waits for the final capture flush. TLS capture
uses `asmcgocall` and never waits for Python: blocking there would stall Go's GC. Outbound request
rewriting is unsupported and the Python API rejects it.

`BOOKS` in `src/procdef.h` is the one table (`pyagy.BOOKS` mirrors it). Addresses come from the
generated `symbols/symbols.json`; candidates for new books are in `symbols/funcmap.tsv.gz`. Go's
amd64 register ABI passes arguments in `RAX, RBX, RCX, RDI, RSI, R8, R9, R10, R11` (a `[]byte`
takes three); `R14` holds the goroutine and must never be clobbered.

## The cgocall-trampoline path

Books use static entry trampolines: a goroutine may park and resume on another thread, so a book
must not depend on its OS thread or rewrite its return address. Never book runtime-special
functions such as `runtime.main`.

The converter patches each target past its stack check (so `morestack` cannot re-enter the stub)
with a jump to a `.plt.agy` entry whose GOT slot `ld.so` binds eagerly to `agy_stub__<id>`. The stub
loads `g_agy_on[book]`; for a non-null handler it saves the Go argument registers into a stack block
and calls `runtime.cgocall` (`AGY_FULLGO`, GC-safe for functions that may park) or
`runtime.asmcgocall` (`AGY_ASMGO`, hot functions that never park). It then restores the registers
and either returns the handler's verdict or replays the original prologue and resumes through
`agy_resume__<id>`.

Go's register ABI has no ordinary callee-saved registers. The stub saves arguments because
the interrupted function still needs its original inputs after the inserted runtime call. Its
Go-stack frame is allocated after the preserved stack check and bounded by the compile-time
invariant `AGY_FRAME <= 128`. The current frame is 112 bytes.

Each `procdef.h` row reads `{id, mode, retcap, AGY_FULLGO(symbol)}` (or `AGY_ASMGO` / `AGY_OFF`).
The generated `symbols_gen.h` holds the expected `AGY_BUILD_ID` and `AGY_STUBS`, one
`X("id", go_frame, reg_ptrmask, stack_args_size, stack_ptrmask)` per stubbed row.
`goframe.cpp` expands it into the naked stub templates, whose asm a `consteval` builder writes
at compile time, and into the `agy_stubs` table of entry and frame labels. `gomod.cpp` uses that
table to register the synthetic moduledata Go needs to unwind and scan the stubs; the frame
layout is in `goframe.h`.

Go scans the pointer maps precisely, including functions marked `FuncFlagAsm`. The generator
reads each target's original `ArgsPointerMaps[0]` and validates the register stores and reloads
around `runtime.morestack_noctxt`. This identifies which saved registers hold pointers and
which pointer arguments remain on the caller's stack. The synthetic locals map marks those
saved register pointers and the initialized `&block` argument. Its argument map retains the
stack-passed pointer bits; uninitialized register spill homes are never marked. Scalar words
must stay unmarked. Unsupported spills, closure contexts and ambiguous layouts fail the build.

The header also contains `AGY_PLANNER_RESPONSE_ITAB`, derived from the validated
`Step.GetPlannerResponse` getter. App-response capture requires both the planner-response
step type and this oneof type identity, then reads the fields verified by
`CortexStepPlannerResponse.GetResponse`. It starts from the step argument in `RDI`; scratch
registers and metadata strings do not supply answer text.

## Diagnostics

| probe | enable | read |
| --- | --- | --- |
| entry-argument object graph | `AGY_PROC_CGT_ARGS` / `ask(..., arg_probe=True)` | `.cgt_args` |
| deduped Go call stacks | `AGY_PROC_STACK` / `ask(..., stack=True)` | `.stacks`, `.call_graph` |
| backend RPC timeline (`CodeAssistClient.*`) | always on | `.rpc_trace` |
| decoded answer at the app boundary (`updateWithStep`) | always on | `.app_text` |

Native stack deduplication uses 1,024 bounded atomic slots shared by concurrent producers.
Stacks need `symbols/funcmap.tsv.gz` (`pixi run shim-symbols`). Offline renderers:
`python3 -m pyagy.agy_process.symbolize <capture.jsonl> [--graph]` and
`python3 -m pyagy.agy_process.rpctrace <capture.jsonl> --stacks`.

## Hybrid tools / mcp_context

agy is an MCP client, so new tools and context come from an MCP server, not from rewriting requests.
`pyagy.config.write_mcp_config(tools, context)` registers `pyagy/agy_mcp_server.py` in
`~/.gemini/config/mcp_config.json`; `config/mcp.json` is the manual template.
`validate_server(timeout=15)` separately checks initialization and the tools/resources lists.
The timeout covers the complete handshake, including partial replies; cleanup terminates and
reaps the temporary server and removes any temporary spec.

`ask()` and `Session` register `tools=` / `context=` under a generated name and remove that
registration on cleanup. With `data_dir=`, these tools and `mcp_servers=` share a private
`<data_dir>/.gemini/config/mcp_config.json`; caller-supplied servers remain there for resume
and inspection. Without `data_dir`, temporary `mcp_servers=` entries use the global config.
Cleanup restores overwritten entries only while their installed values remain unchanged,
preserving edits made during the run. Overlapping registrations in one Python process may
close in either order. Use separate `data_dir` homes for concurrent processes that would
otherwise share server names. `AGY_MCP_CONFIG_PATH` selects the Python writer's path; it is
not an agy CLI configuration-path override.

## Build & run

`pixi install` builds `vendor/libagy.so` and `vendor/agy` (linux-64 only; a missing toolchain fails
the install). [`vendor.json`](vendor.json) pins the release archive and the extracted binary; CMake
verifies both and caches the raw agy in `build/`, and every conversion starts from it;
`-DAGY_BIN=/path/to/raw-agy` selects a local copy. Redistributable (conda) bundles omit both
outputs; `pixi run reconstruct --agy [PATH]` rebuilds them from the pinned agy on the machine (see
the [installation notes](../README.md#redistributable-bundles)).

Run the source tools as `python3 -m symbols.<tool>` from `antigravity/`. LIEF writes the ELF
layout; pyelftools reads and validates its metadata; Capstone validates Go stack checks, frame
prologues, argument spills and the planner-response getter; pwntools assembles book jumps and
`.plt.agy` entries. The Go `pclntab`/`moduledata` decoder remains specific to agy. These build-only
libraries are declared in `[build-system].requires` and pixi's host/workspace dependencies. The source tools stay out of
the wheel; embedded capture modules remain stdlib-only.

```bash
pixi run shim-agy-update  # print the latest release's archive pin for vendor.json
pixi run shim-symbols     # ensure library + metadata are current, then recompile
pixi run shim-books       # after editing src/procdef.h: regenerate pyagy's BOOKS mirror
pixi run build-shim       # rebuild changed outputs
pixi run python test_scripts/test_agy_stubs.py    # after stub or toolchain changes; no model calls
pixi run python test_scripts/test_agy_symbols.py  # parser, resolver, GC maps, generated sources, conversion
pixi run python test_scripts/test_appresponse.py  # native response packets, Unicode, caps, concurrent stacks
pixi run python test_scripts/test_http1sse.py     # request/response matching and SSE decoding
pixi run python test_scripts/test_config.py       # MCP setup, handshake deadlines and child cleanup
pixi run python test_scripts/test_agy_system_message_api.py  # capture paths and MCP/session cleanup
pixi run python test_scripts/test_rewrite.py      # standalone rule utility; no native rewriting
pixi run test_scripts/run-agy.sh --version       # linked startup; writes antigravity.log
```

After source changes, run `pixi reinstall --locked tasksolver` in the consuming workspace
to refresh its packaged native artifacts; editable Python imports alone do not rebuild them.
The checks above need no provider credentials. Missing-artifact skips do not validate a build,
and offline fixtures do not establish authenticated provider behavior.

The native code needs conda GCC 15 (C++23 with constexpr asm strings). The toolchain also supplies
pwntools' GNU assembler/linker and C preprocessor. After a compiler-major change, clear the CMake
caches but keep `build/antigravity-*.raw`.

agy needs a real git workspace (an empty directory hangs at startup); headless sign-in uses an
out-of-band OAuth code paste.

## Capturing model traffic

The model endpoint is HTTP/1.1 + SSE (`POST /v1internal:streamGenerateContent?alt=sse`). The
request comes from the `tls_write` book; response events and usage arrive through `resp_chunk`
(`toStreamResponseChunk`), which observes each decoded SSE data line. `http1sse.py` decodes
them, and `BaseCorrelator.feed_chunk` (`wirecap/decode/`) groups them by response ID. Request
matching distinguishes overlapping answer and session-title calls, including `flash` versus
`flash-lite` models. agy's other connections are HTTP/2 (`h2reassemble.py`); a
`streamGenerateContent` request sent over HTTP/2 is reassembled and paired the same way. The
request summary's `first_user_text` is the latest `<USER_REQUEST>` (falling back to the last
plain user text, then the context summary's last request), and each turn lists its
`functionCall` parts as `tool_calls`. The `smoke` book is recorded once per process.

Native `app_response_packet` and `resp_chunk_packet` events contain a 16-byte `AGTX` version-1
header with the original payload length. The Python router validates and unwraps them before
recording canonical `app_response` events or building `genai_turn` records. Separate packet
kinds keep legacy raw payloads unambiguous, even when their text starts with `AGTX`.

## The `pyagy` client: interact / modify / decode

One call sends a prompt with optional tools, context or system notifications, and returns the
decoded answer with everything the run captured.

```python
from pyagy import ask, Session, ToolSpec, ContextResource

r = ask("Summarize this repo.")                 # one turn; installs the full book union
print(r.text)                                    # clean final answer (prefers app→wire→transcript)
print(r.model, r.usage.total_tokens)             # decoded from the model turn
print(r.request["tools"], r.request["first_user_text"])   # the request as SENT
print(len(r.turns), [t["text"][:20] for t in r.turns])    # every wire model turn decoded
print(r.app_text)                                # answer decoded at agy's consumer boundary
print(r.rpc_trace)                               # labeled backend-RPC timeline

with Session(tools=[ToolSpec("weather", handler="mytools:weather")]) as s:
    print(s.ask("Weather in Paris?").text)       # agy can call the injected tool
    print(s.ask("And in Tokyo?").text)
```

Every run installs the enabled books. A field is empty or `None` when the run captured nothing
of that kind. `.text` prefers a complete app snapshot, then the wire, then the PTY transcript,
and reports which it used in `.source`.

`capture=False` disables JSONL output while decoded events still stream to the parent. A string
`capture=` selects the output path for one-shot calls and sessions; parallel `ask_many()` calls
insert an index before the filename extension so their captures remain separate.

| surface | books | `AgyResponse` field |
|---|---|---|
| wire | egress `tls_write` + `resp_chunk` | `.turns` / `.request` / `.events` / `.usage` / `.model` |
| app boundary | framework consumer (`updateWithStep`) | `.app_text` (+ `.source=="app"`) |
| response capture diagnostics | app and SSE payload framing | `.response_capture_errors` |
| system notifications | native system-step serializers | `.system_messages` / `.system_message_errors` |
| rpc trace | `CodeAssistClient.*` backend RPCs | `.rpc_trace` |
| overlay `stack=True` | Go call-stack unwind at each fire | `.stacks` (rendered) / `.call_graph` (`Counter`) |
| overlay `arg_probe=True` | arg-graph walk at entry | `.cgt_args` (list of reports) |

- **Composable controls:** `tools=` / `context=` add an MCP server;
  `system_messages=` queues native harness notifications before the next model invocation.
- Passing a rewrite specification through `rewrite=` or `Session.set_rewrite()` raises
  `NotImplementedError`; enabling legacy rewrite environment settings is also rejected.
  The standalone rule utility does not modify native traffic.
- App snapshots and SSE JSON lines are captured atomically up to 1 MiB including their header.
  Larger payloads or a smaller `WIRE_MAXCOPY` produce explicit capture errors; incomplete app
  snapshots never replace a complete wire answer. `.response_capture_errors` retains the source,
  available byte counts and error, including with `capture=False`; `AgyModel` returns
  the same list in TaskSolver metadata. Legacy unframed captures remain readable.
- **Introspection:** `from pyagy import BOOKS, by_mech, enabled_books, by_kind, sync_capable,
  DERIVED_KINDS`, the machine-readable mirror of `src/procdef.h`.
- **Env knobs** (the kwargs set them; diagnostics are in the table above):

  | env var | set by | effect |
  |---|---|---|
  | `AGY_PROC_ENABLE` | (auto) | the one gate: install all books |
  | `AGY_PROC_CONV_ID` | (auto) | enable the `file_open` book → `conversation_id` event |
  | `AGY_PROC_H2` / `WIRE_CORRELATE` | — | `=0` disables HTTP/2 reassembly / the turn correlator (shared `WIRE_*` names; legacy `AGY_PROC_*` accepted) |

- **Always linked:** `AgyProcess` launches the linked `vendor/agy` host directly.
  Its `DT_NEEDED` and `RUNPATH` load libagy and the native dependencies; child processes
  inherit no instrumentation preload settings.
- **Additional offline checks:** `test_rpctrace.py` and `test_symbolize.py`.
  `test_client.py` combines offline assertions with a live request; `test_trampoline.py`,
  `test_agyprocess.py` and `test_agy_session.py` also exercise real provider calls. Their
  authenticated checks require a working login; a skip is not a pass.

## System notifications

`AgyResponse.system_messages` contains the native system-message steps observed while agy
assembles model prompts. This includes agy's own notifications and interagent messages as well
as injected hook messages. Each record includes `text`, `event_type`, `capture_source`,
`step_address`, and explicit truncation information. The serializers can revisit history, so
repeated observations are retained. An address identifies a Go object only within that process;
the capture does not infer a conversation id from it.
Malformed capture packets appear in `AgyResponse.system_message_errors`, including when
`capture=False` streams observations without a parent-side capture-file reader.
`AgyModel` also returns both lists in TaskSolver's response metadata under the same keys.

Capture works independently of sending. To observe agy's notifications without registering
an injection hook:

```python
from pyagy import Session

with Session(workspace="/path/to/repo", enable_system_messages=False) as session:
    response = session.ask("Review the current changes.")
    for message in response.system_messages:
        print(message["event_type"], message["text"])
    print(response.system_message_errors)
```

Send a notification with `Session.send_system_message(text, conversation_id=...)`. Omitting the
id targets the session's main agent; an explicit id targets that conversation, including a
subagent. Messages are queued for the target's next `PreInvocation` hook. Queueing does not
wake an idle agent or interrupt a model request already in flight.

```python
with Session(workspace="/path/to/repo") as session:
    message_id = session.send_system_message("The test run has finished successfully.")
    response = session.ask("Review the latest test result.")
    print(session.system_message_status(message_id))
    for message in response.system_messages:
        print(message["event_type"], message["text"])
```

Pass `conversation_id=child_id` to `send_system_message` to target an existing subagent.
Initial notifications can also be supplied to `Session(system_messages=["..."])`, with their
queue ids available in `session.system_message_ids`. For a one-shot call, use
`ask(prompt, system_messages=["..."])` (or `ask_many` for independent runs).

Delivery uses the CLI's
[native command-hook protocol](https://antigravity.google/docs/hooks?tab=cli): a temporary,
session-scoped entry in `<workspace>/.agents/hooks.json` returns `HookSystemMessage` steps.
Unrelated hooks are preserved. `Session(enable_system_messages=False)` disables registration
and sending; chat-mode sessions use that setting. Capture remains available independently.
Closing the session removes its unmodified hook and private queue, including pending messages.
An externally modified hook entry is preserved; invalid or repeatedly changing configuration
can make cleanup raise. Parallel calls still attempt every channel's cleanup.

Queue status `emitted` means the command hook returned the step to agy; it is not a receipt
from the model. Inspect `.system_messages` to verify native serialization. The native step
attributes its content to the harness, but the inspected serializers carry it in a user/context
envelope with a `SYSTEM_MESSAGE` marker. This does not change the provider's system prompt.
agy 1.4.11 crashed while scrubbing hook metadata maps, so notifications carry text
only; queue ids stay in the parent-side status records.

Messages are limited to 256 KiB of UTF-8 text and must fit within the 1 MiB hook-output limit
after JSON escaping. A callback claims each batch before writing it; a crash can leave a
message `claimed`, which is not retried automatically because agy may already have received it.

Checks: `test_system_messages.py` exercises the native reader and decoder,
`test_agy_system_message_queue.py` covers routing and configuration cleanup, and
`test_agy_system_message_api.py` covers response capture and turn boundaries. The authenticated
`test_agy_system_messages_live.py` verifies one-shot and persistent delivery against native
observations and outgoing model requests. All run with `pixi run python test_scripts/<file>`.

## Sessions

By default, `pyagy` and `TaskSolver` pass `--app_data_dir=pyagy`, persisting programmatic
conversations under `~/.gemini/pyagy/` instead of the interactive CLI's `~/.gemini/antigravity-cli/`
(`app_data_dir="antigravity-cli"` or `PYAGY_APP_DATA_DIR=...` overrides it). Keeping `$HOME` and
`~/.gemini` unchanged means `~/.gemini/config/` (MCP servers, skills, rules, plugins) and OS keyring
login are shared natively; `prepare_app_data_dir()` symlinks `antigravity-oauth-token` and
`installation_id` and syncs global `settings.json` preferences while keeping `trustedWorkspaces`
local to `~/.gemini/pyagy/settings.json`.
`Session` is the first-class object (on `wirecap.runtime.session.WireSession`); `ask()` is one-shot
sugar. Both carry a `conversation_id` that `pyagy.resume(cid)`, `pyagy.continue_latest()`,
`pyagy.list_conversations()` and `pyagy.read_transcript(cid)` work with, as do
`AgyProcess(conversation_id=)` and `AgyModel(multi_turn=True)`.

- **Ids** come from the `file_open` book, which reads the uuid out of the store path agy opens;
  without that event the newest `*.db` by mtime wins.
- **Workspace trust** is on by default: agy refuses to start interactively in an untrusted
  workspace and has no flag for it, so each launch adds its workspace to the target store's
  `settings.json → trustedWorkspaces`. `trust=False` opts out, `skip_permissions=True` uses the blunt flag.
- **`data_dir=`** scopes the entire `.gemini` tree to a project. agy hardcodes `$HOME/.gemini`, so
  this overrides `HOME` and links the real login token and global config in, leaving `~/.gemini`
  untouched. Git-ignore the scoped `.gemini/`.

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
prompt tells the agent to open them with `read_file`. Smoke test:
`pixi run python test_scripts/example_agy_backend.py`. Use `pyagy.Session` for multi-turn work.

### Chat mode (`AgyModel(chat=True)`)

To use agy like a chat-completion call (e.g. as a judge on a subscription login), `chat=True`
writes `<workspace>/.agents/agents/tasksolver-chat/agent.md` and runs each call with
`--agent tasksolver-chat --effort low`:

```yaml
tools:
  - read_file                  # the only tool: opens the image files
excludeDefaultComponents: true # no default prompt sections or built-in tools
inheritCustomizations: false   # none of the user's skills, rules, plugins, subagents, MCP servers
mainAgent: true
subagent: false
model: inherit                 # the --model flag decides
commandExecutionPolicy: off
```

Using any other tool, or opening a file it was not handed, raises `ChatModeViolation`; the harness
still offers `manage_task`, and agy still makes its own small session-title call. Checked with agy
1.5.5: the model is offered `read_file` and `manage_task` (13 tools without the agent), and the
first request has ~0.7k input tokens in place of ~14.4k.

The reader's name belongs to the agy release. Up to agy 1.2.x it was `view_file`, with the file in
`AbsolutePath`; agy 1.5 renamed it `read_file`, with the file in `Path`, and a turn checked against
the earlier name fails as `agy --print used read_file in chat mode`. agy 1.5.5 still resolves
`view_file` in `tools` to `read_file`, so a definition that lists both names does not start
(`tool name conflict: multiple tools resolved with name "read_file"`). After an agy upgrade, read
the offered tools and the call's arguments from a capture and update `CHAT_AGENT_MD`, `CHAT_TOOLS`
and `CHAT_PATH_ARG` in `pyagy/model.py` together. Releases before 1.2.11 ignore workspace agents
under `--print`.

## Risks / limitations

- **Locked-down containers:** configure downloads the pinned agy and the pixi installer uses
  GitHub releases; point `-DAGY_BIN=` at a local raw agy if egress is blocked.
- **WSL1 hosts:** the setuptools link step needs the `wsl1-exec.so` shim in `LD_PRELOAD`.
- This instruments a binary **you run on your own machine** for research and interop; it does
  not cross anyone else's security boundary.
