# antigravity — bind custom network / MCP context / tools into `agy`

`antigravity` instruments the **Antigravity CLI** (`~/.local/bin/agy`, Google's agentic
coding tool, internal codename *jetski*) so we can observe and modify its
behaviour from our own **Python** code, in-process — without the source and
without a supported plugin API for what we need.

It does this with two cooperating mechanisms, loaded via a single `LD_PRELOAD`
shared object:

1. **`LD_PRELOAD` interposition** for the few things that actually cross libc in
   this binary (cgo DNS via `getaddrinfo`, `getenv` config injection, and — most
   importantly — being present in-process so we can bootstrap the next part).
2. **frida-gum inline hooks** on recovered Go function addresses, for everything
   Go does *not* route through libc (its own TLS/HTTP stack, MCP manager, tool
   registry, prompt assembly).

Hook events are delivered to an **embedded CPython interpreter** running on a
dedicated worker thread, where your logic lives (`antigravity/pyagy/agy_process/`).

---

## What `agy` actually is (and why it dictates the design)

| Fact (verified) | Consequence for the design |
| --- | --- |
| **Go binary**, cgo-enabled, statically linked except libc/libpthread/**libresolv** | Go makes syscalls *directly*, not through libc → classic `LD_PRELOAD` of `connect`/`send`/`SSL_*` **cannot** see its network traffic. We must hook Go functions in-memory. libresolv ⇒ the **cgo DNS resolver** is compiled in, so `getaddrinfo` interposition *can* redirect where it connects. |
| TLS is **BoringCrypto**, statically linked (no `libssl.so`) | No dynamic `SSL_read`/`SSL_write` to preload. We hook Go's `crypto/tls.(*Conn).Read/Write` instead, which see **plaintext**. |
| **Stripped** (no ELF symtab) but **pclntab intact** (magic `0xf1ffffff`) | Function *names* survive. ~131 844 functions are recoverable. |
| `pcHeader.textStart == 0`; real base is a PIE-relocated `moduledata.text` (`0x4eb5080`, `0x80` past the ELF `.text` addr) | Stdlib `debug/gosym` mis-resolves (uses `textStart`, returns morestack trailers) and **GoReSym can't even locate the pclntab**. We resolve directly: find `moduledata` via the reloc whose addend is the pclntab vaddr, read `moduledata.text`, then `addr = text + func.entryoff`. `textsectmap` has one entry (code is one contiguous Go text section), so the mapping is linear — and every hooked address is **prologue-verified**. |
| Backend is `google.cloud.businessaicode` (Gemini); env overrides exist: **`BAICODE_ENDPOINT_URL`**, `CLOUD_CODE_URL` | Pure *redirection* may need no hook at all — just an env var. |
| Speaks **MCP as a client** (`mcp.json`/`mcpServers`, `GetPluginMCPSpecs`, `SetMcpManager`) and supports **custom agents** | Custom **tools** and **mcp_context** can largely be delivered *natively* by config (the hybrid path); hooks are reserved for what config can't express. |
| Runs on **WSL1** (syscall-translation layer, not a real kernel) | Frida's ptrace/`frida-server` injection is unreliable here. We use **frida-gum *embedded*** (loaded in-process by our `LD_PRELOAD` constructor, `gum_init_embedded()`), which needs no ptrace — only `mprotect`. |

**Build pinning.** Everything is pinned to the agy ELF BuildID (currently
`dee6de740c3883dd05450228aeec8a75`, agy **1.0.16**). The shim refuses to install hooks if
the running binary's BuildID doesn't match `symbols.json`, so offsets can never be
silently applied to a different build. Re-run the extractor after any `agy` update
(`make symbols`).

---

## Architecture

```
        ┌─────────────────────────── agy process ───────────────────────────┐
        │                                                                    │
        │   Go runtime + goroutines (net/http, crypto/tls, MCP, tools)       │
        │        │  ▲                                                        │
        │  inline│  │ (return, possibly-rewritten args)                      │
        │   hook │  │                                                        │
        │        ▼  │                                                        │
        │   ┌─────────────────┐   tiny C, on goroutine stack:                │
        │   │ gum trampoline  │   copy buffer + build request                │
        │   └───────┬─────────┘                                              │
        │           │ enqueue (+ block if SYNC)                              │
        │           ▼                                                        │
        │   ┌───────────────────────────────────────────┐                   │
        │   │ pyworker pthread  (LARGE 16MB stack)        │  ← libpython3.13  │
        │   │  Py_InitializeEx(0); import pyagy.agy_process │    lives only     │
        │   │  loop: pop req → PyGILState_Ensure →        │    here (one      │
        │   │        call on_tls_write/read/http/tool →   │    PyThreadState) │
        │   │        write verdict → signal condvar       │                   │
        │   └───────────────────────────────────────────┘                   │
        └────────────────────────────────────────────────────────────────────┘
             ▲ LD_PRELOAD=antigravity.so  (constructor: gum_init_embedded,
                                       verify build-id, load symbols.json,
                                       install hooks, spawn pyworker)
```

### Why a dedicated worker thread with a big stack

gum inline hooks fire **on whatever stack the hooked Go function was using — a
goroutine stack**, which starts ~8 KB and is *movable*. Calling libpython (deep C
stacks, arbitrary Python) directly there risks a stack overflow into the
goroutine's guard page (a hard crash — Go's `morestack` growth only triggers at
Go prologues, never inside our C code).

So the **goroutine-side hook body stays tiny** (copy the `[]byte`, push a
request) and all Python runs on a dedicated pthread with a normal large stack.
That thread is *not* a goroutine and is unknown to Go's scheduler, so Python's
deep stacks and the GIL never touch Go's runtime.

### Async vs sync (logging vs modify)

Each hook is configured `ASYNC` or `SYNC`:

- **ASYNC** — copy + enqueue + return immediately. Zero stall. Used for logging
  and recording (the current priority: capture traffic for analysis/plots).
- **SYNC** — enqueue then **block on a per-request condvar** until `pyworker`
  returns a verdict; if Python returned replacement bytes, rewrite the buffer
  **in place** (same-or-shorter length) and adjust the length register, then
  return into agy. Used for modifying requests/responses.

**GC-stall caveat.** While a SYNC hook blocks, Go still believes that goroutine
is "running", so it cannot be preempted / its stack can't be scanned until we
return. A slow SYNC callback can therefore stall Go's GC. **SYNC callbacks must
be fast and CPU-bound** (buffer edits, µs–ms). For anything slow (a network
round-trip to a rewrite service), log ASYNC and rewrite on a later turn.

---

## Hook set (v1)

Addresses come from `symbols/symbols.json` (resolved by GoReSym, pinned to
build-id). Names are stable across Go versions; offsets are not.

| Function | Go signature (register ABI) | Purpose | Mode |
| --- | --- | --- | --- |
| `crypto/tls.(*Conn).Write` | `(c *Conn=RAX, b.ptr=RBX, b.len=RCX, b.cap=RDI)` | plaintext **egress** to LLM backend | ASYNC (→SYNC for rewrite) |
| `crypto/tls.(*Conn).Read` | `(c=RAX, b.ptr=RBX, b.len=RCX)` → `n=RAX` on return | plaintext **ingress** from backend | ASYNC |
| `net/http.(*Transport).RoundTrip` | `(t=RAX, req *Request=RBX)` → `resp=RAX` | HTTP-level URL/headers (structured view) | ASYNC |
| *(catalog)* MCP/tool/prompt funcs | see `symbols.json.catalog` | reserved for tool/context injection | — |

**Go register ABI (amd64, Go 1.17+).** Integer/pointer args go in
`RAX, RBX, RCX, RDI, RSI, R8, R9, R10, R11`; results in the same order. A `[]byte`
occupies three consecutive slots (ptr, len, cap). `R14` holds the goroutine `g`
pointer — **never clobber it**; gum saves/restores the full register set around
our callback, so reading is safe.

The TLS layer gives us HTTP/2 frames (HPACK-compressed headers, possibly gzipped
bodies, likely gRPC/protobuf). Reassembly to LLM request/response JSON happens
**in Python** (`pyagy/agy_process/h2reassemble.py`) — decoupled from agy internals, so it
survives agy updates.

---

## Parking functions: the cgocall-trampoline path

The v1 hooks above are gum `Interceptor` attaches — fine for **CPU-only** functions (they
run and return on the same OS thread). But agy's most useful boundaries —
`backend.(*ServerBackend).SendUserMessage` (the fully-decoded model **request**) and
`backend.(*callbackStreamer).Send` (decoded **response** chunks to the UI) — **park** the
goroutine (blocking I/O, mutex/cond). A gum attach rewrites the return address and tracks it
*per-OS-thread*; when a parked goroutine resumes on a **different** M, the intercepted return
never matches → agy silently stalls (which is why a gum attach on a parking func is disabled
— `AGY_OFF` — and these targets are trampolined instead).

**Approach A (`src/cgotrampoline.c` + `src/gomod.c`)** avoids gum's return-tracking
entirely. It redirects the target — **past its stack-check prologue** — into a generated
trampoline that:
1. snapshots the Go-ABI arg registers into a stack **block**,
2. `CALL runtime.cgocall(fn=agy_cgo_hook, arg=&block)` — Go's own Go→C gateway, which switches
   to the g0/system stack and runs our C hook in a safe cgo context (`entersyscall`, P-handoff,
   GC coordination); the hook copies + enqueues to the pyworker, exactly like the ASYNC path,
3. restores the registers, runs the overwritten original instructions, and `jmp`s back into
   the target body.

Because the trampoline never rewrites *our* return address, parking/rescheduling is
unaffected — the goroutine may resume on any M. A hook picks its Go→C gateway *per hook*
via the `MECH` column: `AGY_FULLCGO` uses full `runtime.cgocall` (the robust default —
`entersyscall` + P handoff, GC-safe); `AGY_ASMCGO` uses `runtime.asmcgocall`, a lighter
variant (just the g0 stack switch, no `entersyscall`/`_Gsyscall`) for hot or
syscall-at-entry-sensitive funcs. Both share one region + synthetic moduledata; the pcsp
matches the full-cgo geometry, since only those slots are ever GC-unwound.

**Synthetic moduledata (`gomod.c`).** `cgocall`'s `entersyscall()` opens a GC-scannable window
over our trampoline frame; if Go's unwinder can't `findfunc` the trampoline PCs it
`throw("unknown pc")`s. `agy_gomod_register()` installs a **synthetic moduledata** whose
pclntab/pcsp cover the trampoline, so GC unwinds it cleanly — and the pcsp must report **one
constant spdelta** across the whole trampoline-frame window.

**The GH_SPILL geometry (the fix that made the cgocall variant correct).** `runtime.cgocall`
uses Go's internal ABI and spills its two register args (`fn`, `arg`) into **caller-provided
slots at `[S]` and `[S+8]`** (`S` = rsp at the `call`). So the block must **not** sit at `S`,
or cgocall overwrites `block.kind`(@`[S]`) and `block.regs.rax`(@`[S+8]`) before the hook reads
them — the symptom was `os.Getenv` returning `""` → `$HOME is not defined` at startup. The fix
reserves **16 dead bytes (`GH_SPILL`) below the block**, baked into the **fixed** frame
(`GH_FRAME = 16 + 96 block + 8 pad = 120`), *not* a transient `sub` around the call — a
transient sub would make the real spdelta disagree with the synthetic pcsp inside the
`_Gsyscall` window → `throw("unknown pc")`. cgocall always spills exactly 2 slots, so 16 bytes
is exact and future-proof. (Directly confirmed under gdb — see Status.)

These trampoline targets (`SendUserMessage`/`Send`, the framework consumers, and the
`CodeAssistClient` RPCs) are marked `AGY_FULLCGO`/`AGY_ASMCGO` in `procdef.h` and installed as
one set — `install_hooks` collects them into a **single** `agy_gohook_install` call (the `gomod.c`
singletons make a second call unsafe). The GC-safe synthetic moduledata is always installed.
`os.Getenv` was the benign CPU-only validator for this path during bring-up (a trampoline on
it that answers cleanly proves the tool sound); it's now `AGY_OFF` because the gum `smoke`
hook already covers `os.Getenv` and the real targets exercise the mechanism. (Two earlier
bring-up rungs — a naive gum attach on the parking funcs, and a `runtime.cgocall` gateway
probe — were removed once this path proved out; the trampoline is park-safe precisely because
it never intercepts the return.)

### App-boundary text probe — why the wire (`http1sse`) wins for the response

We evaluated hooking agy's **app boundary** to get the model response as pre-decoded Go
values instead of parsing HTTP/1.1+SSE. The `AGY_PROC_CGT_ARGS` diagnostic (a fault-safe
`process_vm_readv` recursive object-graph string-finder in `cgotrampoline.c`, gated on that env; the
report lands as a `cgt_args` capture event) let us dump any trampoline hook's argument graph
and reverse-engineer where text lives. Fanning out over the **whole** enumerated model-text
pipeline (`gemini_coder/framework/{generator,core}` + `jetski/language_server/modelapi*`, not
just the `jetski/cli/backend` tail) settled it:

- **`callbackStreamer.Send` (a trampoline hook) is the pipeline tail** — it only ever sees the response
  already wrapped in a noisy `exa.jetski_cortex_pb.AgentStateUpdate` proto (text at a deep,
  per-build-fragile offset like `rbx+56+0`, amid tool schemas / cost categories / re-sent
  context). The clean text flows through the framework *upstream* of it.
- **Leaf getters that return the delta as a plain Go string** (`GetChatMessageResponse.GetDeltaText`,
  `CompletionDelta.GetDeltaText`; gum-attach `on_leave`, zero struct-offset fragility)
  **don't fire** — they belong to the ccpa/codeium provider path, but agy 1.0.16 uses the
  gemini/cloudcode provider.
- **Provider-agnostic framework choke points** (`streamResponseHandler.{finalizePlannerResponse,
  updateWithStep,processStream}`, `core.createPlannerResponseStep`; trampoline) **fire**,
  but the assistant **output** text is *built during* the call — so it is a return value /
  post-call state, **not** in the entry-arg registers the trampoline reads (a depth-8 walk of
  `rax`→`rsi` surfaced only the *input* context/config/tool-schemas, never the answer).

Net: the entry-arg trampoline structurally can't see text assembled during the call, and the
functions that *return* it as a clean string are the inactive provider's. `crypto/tls.(*halfConn).decrypt`
+ `http1sse` already decode the exact output cleanly and stably off the wire, so **the response
data path stays on the wire**; stages 11–12 remain as diagnostic/RE scaffolding
(`AGY_PROC_CGT_ARGS` re-derives the layout in one command on a future agy build — from the
client that's `ask("…", arg_probe=True).cgt_args`, one rendered report per fire).

### The return-value problem — can we capture a response the trampoline built *during* a call?

The trampoline reads ENTRY args, never the return, so values *built during* a call (the assembled
response) aren't in its registers. Two general return-capture designs were assessed:
- **`on_leave`-style return-address redirect keyed by g — BLOCKED.** Moving the real caller return
  address off-stack breaks Go's `gentraceback`/`copystack` during a park (it can't reach the real
  caller); no synthetic pcsp restores info that's gone.
- **In-place `ret`-patch (capture RAX/RBX at the `ret`, post-park) — VIABLE-with-work** (reuses the
  proven cgocall-window moduledata; blockers are ret-site discovery + the 1-byte-`ret` patch size).
  Not built — kept as the future general escape-hatch.

Instead we take the **consumer-entry hook**: Go passes a result forward as an argument, so hook the
*consumer* whose entry arg is the produced value. To pick the *best* consumer we mapped the complete
ordered chain from where `http1sse` observes (the TLS read) up to the terminal observer, which crosses
**two goroutines joined by a channel**:

- **PRODUCER** (wire → decoded chunk → channel): `crypto/tls.(*Conn).Read` → `net/http` de-chunk →
  `bufio.Scanner.Scan` → `codeassistclient.ProcessStreamChunks` → `getStreamingTextCompletion` →
  push to channel.
- **CONSUMER** (channel → assemble → build `*Step` → commit → observers): `AgentExecutor.Run` →
  `Executor.Execute` → `runExecution` → `PlannerGenerator.Generate` → `attemptGenerate` →
  `streamResponseHandler.processStream` (pulls the channel) → **`updateWithStep`** → `finalizePlannerResponse`
  (builds the full response, RETURNS it) → up to `runExecution`, which wraps it in a fresh `*Step` and
  calls `executor.(*ExecutionTrajectory).AppendStep` → observers → `AgentState.OnStepsChanged` (last).

`OnStepsChanged` (the terminal observer) *does* carry the full answer, but 6 struct-hops deep into
`AgentState` internals behind a mutex (`rax+24+32+16+48+32+32`) — fragile, and a "longest prose"
heuristic there is unreliable (a 3.5 KB tool-schema JSON in the same arg is longer than the answer).
The live `AGY_PROC_CGT_ARGS` probe found the **shallow** consumer: **`generator.(*streamResponseHandler).updateWithStep`** (stage 12) — its `RSI` arg points at the planner response, and the assistant
text is a Go string at **`+0x8`(ptr)/`+0x10`(len), a single deref**, the stable cortex proto layout
(thinking sits deeper at `+0x28`). `agy_cgo_hook` decodes that directly to an **`app_response`** event
(the text-bearing fires carry the *full* answer, not per-delta fragments; empty-response fires are
skipped by the length check). `AppendStep` is committed too (it fires on the `--print` path and anchors
the chain) but its `*Step` text is 6 hops deep, so the clean decode lives on `updateWithStep`.

This finally makes "prefer the app boundary for the RESPONSE" real: `updateWithStep` fires on every
run (it's in the hook union), so `AgyResponse.source == "app"` and `.app_text`/`.text` come from the
answer decoded at agy's own consumer boundary (one deref, no HTTP/SSE reassembly). The wire
`genai_turn`/SYNC-rewrite is captured in the *same* run; when no `app_response` is present the client
falls back to the wire turn and then the PTY transcript (`.source` `"wire"`/`"transcript"`).
`http1sse` therefore remains the authoritative source for the structured request/usage.

### Call-stack probe (`AGY_PROC_STACK=1`) — mapping HOW agy assembles a turn

To understand the pipeline (not just the bytes), the shim can dump the **Go call stack** at
every hook fire. `agy_backtrace` (cgotrampoline.c) walks the frame-pointer chain (`[rbp]`=saved rbp,
`[rbp+8]`=return addr — Go keeps frame pointers) with **fault-safe `process_vm_readv`** reads,
emitting each return PC as a link vaddr (`pc - base`). `build_symbols.py` writes a full sorted
`symbols/funcmap.tsv.gz` (addr→name over all ~132k funcs; gitignored, regenerated by `make
symbols`), and `pyagy/agy_process/symbolize.py` bisects it to render named stacks + an
aggregated caller→callee **call graph**. Hot hooks repeat the same few stacks, so emission is
**deduped** (each distinct stack once) — without that, a per-fire emit floods the worker queue
and stalls the turn. gum hooks (tls_write/decrypt) fire post-prologue → complete stacks;
trampoline hooks capture the *caller's* rbp → the chain starts one frame above the target.

Observed on agy 1.0.16 (turn completes; ZORPLE unaffected). The **assembly pipeline** (stage 12,
one goroutine) — this is how the answer is produced:

```
cortex.(*CascadeManager).executeOne            (agent-turn driver, on a background.Pool goroutine)
  → agentexecutor.(*AgentExecutor).Run
  → executor.(*Executor).Execute → runExecution → runInvocation
  → generator.(*PlannerGenerator).Generate → generateWithModelOutputRetry → generateWithAPIRetry → attemptGenerate
  → generator.(*streamResponseHandler).processStream → updateWithStep* / finalizePlannerResponse
```

And the **wire I/O** (stage 12 hooks the app goroutine; stage 3 the transport) — note agy's
HTTP client concurrency: egress is written on a *decoupled* `writeLoop` goroutine while the
response is read *inline* on the app goroutine, so the app caller shows up on the read stack:

```
egress  crypto/tls.(*Conn).Write  ← net/http.(*persistConn).writeLoop ← Request.write ← transferWriter.writeBody   (dialConn.gowrap3 goroutine)
ingress crypto/tls.(*Conn).Read   ← net/http.(*persistConn).Read ← chunkedReader ← compress/gzip.(*Reader).Read
        ← io.ReadAll ← codeassistclient.doRequestAndUnmarshal ← …RecordConversationOffered / getStreamingTextCompletion.func1
```

From the client: `ask("…", stack=True).stacks` returns the symbolized, grouped
stacks and `.call_graph` the caller→callee edge `Counter`. Low-level path:
`AGY_PROC_STACK=1 test_scripts/run-agy.sh --print "…"`,
then `python3 -m pyagy.agy_process.symbolize <capture.jsonl> [--graph]`. `.stacks` needs the
funcmap (`make symbols`); when it's absent the accessor returns a short reason string.

### RPC trace — the app-level backend timeline

`(*CodeAssistClient).*` (jetski/language_server) is agy's single client to the CloudCode
backend — each method is one **named** RPC with typed proto args. The union trampolines
them (they park on the HTTP round-trip) so each fires an `rpc_<name>` event, giving a
labeled, time-ordered timeline of a turn that sits alongside the wire `genai_turn` decode.
From the client: `ask("…").rpc_trace` returns the rendered timeline; compose
with `stack=True` (call stack per RPC → `.stacks`) and `arg_probe=True` (walk the request
proto at entry → `.cgt_args`). Low-level path: `python3 -m pyagy.agy_process.rpctrace
<capture.jsonl> --stacks`.

Observed (agy 1.0.16, one turn) — note the concurrency: background loops interleave with the
model turn, and `StreamGenerateContent` **is** the model call:

```
+0.0s  FetchLoadCodeAssistResponse / FetchUserInfo        (startup)
+1.5s  RetrieveUserQuotaSummary   ← store.(*Manager).quotaRefreshLoop        (background)
+1.7s  ListExperiments / FetchAvailableModels ← experiments.(*MendelStateCache).pollLoop
+2.1s  StreamGenerateContent  (THE MODEL TURN) ← codeassistclient.(*CodeAssistClient).GetChatMessage.func3
+3.4s  RecordConversationOffered / RecordTrajectorySegmentAnalytics ← AgentExecutor.recordTelemetryAfterExecution
```

This is the best **request / RPC-level** boundary (clean, app-named, one hook family). The
same entry-only-trampoline limit applies to the *response* (a return value built during the
call) — so responses stay on the wire (`http1sse`); stage 13 is the *what-agy-calls* view.

---

## Hybrid tools / mcp_context

`agy` is an MCP *client*, so the robust way to add custom **tools** and
**mcp_context** is to register a custom MCP server. This is the **additive** modify
path (the SYNC egress rewrite below can only *substitute* equal-length bytes — it
can't grow the request, so new tools/context must come this way). agy 1.0.16 reads
**`~/.gemini/config/mcp_config.json`** (schema `{"mcpServers": {name: {command,
args, env}}}`); `pyagy.config.write_mcp_config(tools, context)` renders a spec and
registers `pyagy/agy_mcp_server.py` against it (merging, preserving other servers),
and `validate_server()` runs the initialize/tools-list handshake as a pre-flight.
`config/mcp.json` is the manual template. Hooks remain the fallback for what config
can't do (injecting context invisibly into prompt assembly, or overriding a tool
result). See `config/` and `pyagy/config.py`.

---

## Layout

```
antigravity/
  README.md                 ← this file (the design)
  Makefile                  ← one build entry: vendor deps (agy, frida-gum) + compile
  vendor/                   ← agy copy (164 MB) + frida-gum devkit + built shim (gitignored; `make setup`)
  symbols/
    build_symbols.py        ← authoritative resolver: pclntab + moduledata.text →
                              symbols.json; self-verifies every hook is a prologue
    gen_symbols_header.py   ← symbols.json → symbols_gen.h (build-id + name→vaddr)
    patch_agy_wsl1.py       ← WSL1: clear tcmalloc MAP_FIXED_NOREPLACE in the fetched agy
    symbols.json            ← {build_id, text_base, hooks:{name→vaddr}, catalog}
  src/
    antigravity.c               ← LD_PRELOAD constructor + gum install + Go-ABI hook
                              callbacks + getaddrinfo interposer
    pybridge.c/.h           ← embed libpython, pyworker thread, queue, dispatch
    procdef.h                ← declarative hook table (id, symbol, mode, kind, mech, leave)
  pyagy/                    ← the `pyagy` Python package (importable; ships in the pkg)
    __init__.py             ← lazy exports (PEP 562): ask/Session/AgyResponse/specs,
                              AgyModel, run_print/InteractiveSession, write_mcp_config
    client.py               ← public API: ask() (one-shot) + Session (multi-turn) +
                              AgyResponse + ToolSpec/ContextResource/RewriteRule/Usage
    config.py               ← MCP config-injection (write_mcp_config/validate_server)
    model.py                ← AgyModel (prepare_payload/ask/rough_guess/run_once/…)
    agyprocess.py           ← THE single agy launcher (SpawnProcess): plain-CLI (read the
                              PTY transcript) or embedded-worker (Python target inside agy);
                              always instrumented on the pinned vendor/agy
    session.py              ← run_print (one-shot dict) + InteractiveSession — thin AgyProcess wrappers
    _term.py / _pty.py / _env.py ← shared PTY glue (ANSI+query responder, spawn+pump,
                              instrumented env) — the building blocks under AgyProcess
    agy_process/__init__.py ← dispatch() → on_tls_write/on_tls_read/on_http/on_dns/on_smoke
    agy_process/http1sse.py ← HTTP/1.1 + SSE decoder for the MODEL endpoint (the right
                              one — the model turn is not HTTP/2)
    agy_process/capture.py  ← live correlator: pairs request↔response across the
                              *Conn/*halfConn stream-id split → genai_turn events
    agy_process/rewrite.py  ← SYNC egress rewrite registry (equal-length, mtime reload)
    agy_process/hooks.py    ← machine-readable mirror of procdef.h
    agy_process/record.py   ← JSONL recorder for plotting
    agy_process/h2reassemble.py ← HTTP/2 + HPACK + gzip reassembly (agy's OTHER conns)
    agy_mcp_server.py       ← stdio MCP server (built-in stub or AGY_MCP_SPEC-driven)
  config/
    mcp.json  agents.json  README.md  ← native custom MCP server / agent (hybrid path)
```

The shim's in-process module is **`pyagy.agy_process`** (loaded by `antigravity.so` via
`AGY_PROC_MODULE`, which defaults to it). Test/experiment drivers live in the repo's
`test_scripts/`: `run-agy.sh`, `agy_session.py`, `analyze_capture.py`, `example_agy_backend.py`.

## Build & run

`pixi install` **builds the shim** — the tasksolver package build (`setup.py` →
`build_py`) runs `make -C antigravity`, which vendors the deps and compiles
`antigravity/vendor/antigravity.so`. Since the shim is x86-64-specific and a main
target, the package is **arch-specific (linux-64)** (`[tool.pixi.package.build.config]
noarch=false`) and the build is **required** — it fails loudly if the toolchain
(gcc / frida-gum / libpython) is missing. Set `ANTIGRAVITY_SKIP_BUILD=1` to build
just the Python library without the shim. (`make`, `hpack`, and `brotli-python` are
pixi/conda deps — no `pip install` needed; this host has no system `make`.)

pixi caches the package build, so **after an agy update** rebuild the shim explicitly
(GNU make comes from the pixi env, so run it via `pixi run`):

```bash
pixi run make -C antigravity symbols   # re-resolve symbols.json from the new agy binary
pixi run make -C antigravity           # recompile the shim (also runs `make setup` first)
```

**libpython / Python version.** The shim embeds the **pixi env's libpython 3.13**
(`$(CONDA_PREFIX)/bin/python3-config`), unified with the pixi-3.13 driver so the
multiprocessing/pickle wire between them is same-version (see *AgyProcess* below). It links
`-Wl,-rpath,$(CONDA_PREFIX)/lib` so agy finds `libpython3.13.so.1.0` at runtime (agy is always
launched via `pyagy/_env.py` under the pixi env). Build with `pixi run make -C antigravity`
(needs `gcc` + `/usr/include/linux/limits.h` from the conda kernel-headers dep). `make setup` fetches the
**frida-gum devkit** from GitHub releases; where GitHub egress is blocked (e.g. a locked-down
cloud container), vendor it manually into `vendor/frida-gum/` (`libfrida-gum.a`, `frida-gum.h`,
`VERSION`) and `make` finds it. `pyagy.agy_process` needs `hpack`/`brotli` only for the
HTTP/2 body decode (both are guarded/lazy), so it imports fine without them.

Run agy under the hook via the launcher in `test_scripts/` — it installs the full working
hook union (gum wire hooks + cgocall-trampoline app/rpc hooks, each hook's cgo gateway
chosen per the `procdef.h` `MECH` column) in one pass; the GC-safe synthetic moduledata is
always on:

```bash
test_scripts/run-agy.sh <normal agy args...>   # capture request+response+app+rpc (authenticated agy)
python3 test_scripts/analyze_capture.py agy-capture.jsonl --plot traffic.png
```

## Status (originally WSL1; re-validated on cloud Linux real kernel — agy 1.0.15 build 1d164dd9…, then re-pinned + re-verified on 1.0.16 build dee6de74…)

- ✅ libpython embeds in-process; worker thread runs; `pyagy.agy_process` imports.
- ✅ frida-gum **embedded** inline hooking works on WSL1 (no ptrace).
- ✅ Hook fires on a real Go function (`os.Getenv`), reads register-ABI string
  args, marshals to Python, records to JSONL — agy runs to completion, no crash.
- ✅ `on_leave` (return-address rewrite, used by `tls_read`) is safe on ordinary
  Go funcs (100+ calls, no `unexpected return pc`).
- ✅ **Interactive session driving works** (`test_scripts/agy_session.py`): under a PTY
  (with a terminal-query responder) we ran a real multi-turn session against the
  logged-in agy — `"what is 2+2"` → `4`, then `"×10"` → `40` — reading agy's
  rendered output and injecting follow-up input (independent of the capture hooks).
- ✅ **In-process model-request capture works.** Hooking
  `crypto/tls.(*Conn).Write` **past the stack-check prologue** (per-func `skip`)
  captures the full Gemini request (HTTP/2 + JSON) with no stall — validated:
  the prompt + `<USER_REQUEST>` framing + context show up in the captured egress.
- ✅ **Response captured in-process too**, via `crypto/tls.(*halfConn).decrypt`
  (CPU-only, runs after the read parked → doesn't park itself). `tls_read` /
  `RoundTrip` / `http2.(*pipe).Write` all *park* (netpoll/mutex/cond) and stall
  agy even past-prologue/on-enter-only, so we hook the decrypt, not the read.
- ℹ️ agy needs a **real git workspace** — an empty dir hangs at startup. It calls
  `daily-cloudcode-pa.googleapis.com` (Gemini) directly from the CLI process.

**Three hard-won lessons.** (1) Never hook runtime-special functions
(`runtime.main` → `unexpected return pc`). (2) Hook **past the stack-check
prologue** or Go's `morestack` re-enters the trampoline and stalls (per-func
`skip`). (3) Never hook a function that **parks** the goroutine (blocking I/O,
mutex/cond contention, channels) — gum's return-tracking is per-OS-thread, and Go
resumes the goroutine on a *different* thread, so the intercepted return never
matches → silent stall (even past-prologue, even on-enter-only). **Corollary /
the capture rule:** hook the **crypto** step (encrypt/decrypt — CPU-only, runs
before/after the parking I/O), never the **I/O** step (`Write`/`decrypt` ✓,
`Read`/`socket write` ✗).

### Cloud Linux (real kernel 6.18.5) re-validation — cgocall trampoline

The cgocall-trampoline path was re-verified end-to-end on a **real-kernel** cloud container
(not WSL1), agy 1.0.15 (`build 1d164dd9…`), with the shim built by the **system toolchain**:

- ✅ **Builds + loads on a real kernel** — build-id verified, synthetic moduledata
  (`frame=120`) prepared, trampoline installed. The `mprotect(PROT_EXEC|PROT_WRITE)` W^X
  concern did **not** materialize here.
- ✅ **Live-turn matrix** (`agy --print` under a PTY): stage 9 (`os.Getenv`, 234 trampoline
  transitions) and stage 8 (`SendUserMessage`=1 + `callbackStreamer.Send`≈20) both **complete
  the turn** — cgocall == asmcgocall == no-hook baseline, with zero `throw`/`unknown pc`/panic/
  `UnicodeDecodeError` and zero `$HOME is not defined`.
- ✅ **gdb direct root-cause proof** (impossible on WSL1, where gdb couldn't run): breaking
  inside `runtime.cgocall` for our invocation shows its prologue spilling `fn→[S]`, `arg→[S+8]`
  (`mov %rax,0x38(%rsp)`/`mov %rbx,0x40(%rsp)`); with the fix `&block − S == 16`, so after the
  spill `block.kind`(→`"cgt_getenv"`) and `block.regs.rax` are byte-for-byte intact — the two
  spill slots land entirely in the dead `GH_SPILL` scratch. Pre-fix (`&block==S`) they'd be
  clobbered.
- ℹ️ **Headless login in a cloud container:** agy's Google OAuth uses an out-of-band
  code-paste flow (`redirect_uri=antigravity.google/oauth-callback`), so sign-in needs no
  in-container browser — open the URL elsewhere, paste the `4/…` code back. All Google/Gemini
  endpoints (`oauth2.googleapis.com`, `daily-cloudcode-pa.googleapis.com`) are reachable.
- ⚠️ **Harness gotcha:** the model's silent "thinking" gap can exceed a short read-idle
  window, so a too-aggressive idle cutoff looks like "no answer" — widen it or wait for
  process exit.

## Capturing model traffic

**In-process request capture works.** Two hazards had to be
solved, both validated on full turns that reach `daily-cloudcode-pa.googleapis.com`:

1. **morestack re-entry.** Hooking *at* a func entry: when the frame grows the
   stack, Go's `morestack` re-runs the entry and re-enters our trampoline →
   stall. **Fix:** attach **past the stack-check prologue** — `build_symbols.py`
   computes a per-func `skip` (finds `cmp 0x10(%r14),reg` + the `Jcc`), the shim
   attaches at `base+vaddr+skip`. Args are still in registers there.
2. **park-while-hooked.** A hooked func that *parks* the goroutine on blocking
   I/O stalls agy even past-prologue and on-enter-only. `crypto/tls.(*Conn).Write`
   doesn't park → **safe**; `crypto/tls.(*Conn).Read` and `net/http.RoundTrip`
   park → **stall** (disabled — `AGY_OFF` in procdef.h).

**The rule (proven both directions):** hook the **crypto** step (CPU-only, runs
after the I/O) — never the **I/O** step (which parks). So:
- **Request:** `crypto/tls.(*Conn).Write` (encrypt side) — captures the model
  request: HTTP/2 + JSON (`"role":"user","parts":[{"text":"<USER_REQUEST>…"`, plus
  system prompt/context/tools in the larger frames).
- **Response:** `crypto/tls.(*halfConn).decrypt` (decrypt side, `on_leave` []byte)
  — captures the decrypted inbound records. It runs *after* the socket read
  parked-and-resumed, on ciphertext already in memory, so it's CPU-only and
  doesn't park (validated: no stall, agy completes, ~150 KB inbound captured).

Both are gum-attach hooks in the union. `Read`/`net/http.RoundTrip`/`http2.(*pipe).Write` all **park**
(netpoll / mutex / cond) and stall agy even on-enter-only past-prologue — that's
why we hook decrypt, not read. `AGY_PROC_PREVIEW` sets capture bytes/event.

**The model endpoint is HTTP/1.1 + SSE, not HTTP/2** (a load-bearing finding):
`POST /v1internal:streamGenerateContent?alt=sse HTTP/1.1` to
`daily-cloudcode-pa.googleapis.com`, reply a `text/event-stream` of
`data: {...}` lines (Gemini candidates wrapped under a `response` key). So
`http1sse.py` — not `h2reassemble.py` — is the right decoder for it:
de-chunk + gzip/br/deflate inflate + SSE assemble → text/usage/finishReason.
Two wire facts it handles: requests use **keep-alive** (`streamGenerateContent`
reuses the `loadCodeAssist` connection, so it isn't a first-write), and the
decrypt stream carries a **TLS-handshake plaintext prefix** before the status line.
`capture.py` correlates the request (egress `tls_write`, keyed by `*Conn`) with its
response (ingress `decrypt`, keyed by `*halfConn` — a *different* stream id) by
time+host and emits a merged **`genai_turn`** event with the full decoded request
and response text. `h2reassemble.py` still applies to agy's *other* (gRPC/HTTP-2)
connections. (The PTY transcript / `AgyModel` also give the reply if you don't need raw.)

CPU-only funcs (`os.Getenv`, `(*RootModel).Serialize`, `proto.Marshal`) also hook
cleanly; the app-layer R&D examples show the `[]byte`-return capture (`on_leave`
RAX=ptr, RBX=len).

## The `pyagy` client: interact / modify / decode

`pyagy` exposes one end-user API that folds the pieces above (PTY driver + capture
correlator + rewrite registry + config-injection) into a single call. You send a
request, optionally **modify** the raw prompt/tools/context, and get back a decoded
string/JSON response.

```python
from pyagy import ask, Session, ToolSpec, ContextResource, RewriteRule

r = ask("Summarize this repo.")                 # one turn; installs the full hook union
print(r.text)                                    # clean final answer (prefers app→wire→transcript)
print(r.model, r.usage.total_tokens)             # decoded from the model turn
print(r.request["tools"], r.request["first_user_text"])   # the request as SENT
print(len(r.turns), [t["text"][:20] for t in r.turns])    # every wire model turn decoded
print(r.app_text)                                # answer decoded at agy's consumer boundary
print(r.rpc_trace)                               # labeled backend-RPC timeline
print(ask("…", stack=True).stacks)               # symbolized Go call stacks (overlay)
print(ask("…", arg_probe=True).cgt_args)         # trampoline arg-graph reports (overlay)

with Session(tools=[ToolSpec("weather", handler="mytools:weather")]) as s:
    print(s.ask("Weather in Paris?").text)       # agy can call the injected tool
    s.set_rewrite([RewriteRule("Paris", "Tokyo")])  # live, equal-length substitution
    print(s.ask("And there?").text)
```

**One call, one result object.** Every run installs the full working hook union, so a
single turn populates all capture surfaces at once; `stack=`/`arg_probe=` are optional
diagnostic overlays on top. Each `AgyResponse` field is empty/None when this run didn't
capture that kind (e.g. no `rpc_*` events):

| surface | hooks | `AgyResponse` field |
|---|---|---|
| wire *(the `rewrite=` surface)* | egress `tls_write` + ingress decrypt | `.turns` / `.request` / `.events` / `.usage` / `.model` |
| app boundary | framework consumer (`updateWithStep`) | `.app_text` (+ `.source=="app"`) |
| rpc trace | `CodeAssistClient.*` backend RPCs | `.rpc_trace` |
| overlay `stack=True` | Go call-stack unwind at each fire | `.stacks` (rendered) / `.call_graph` (`Counter`) |
| overlay `arg_probe=True` | trampoline arg-graph walk at entry | `.cgt_args` (list of reports) |

- **Two modify mechanisms, composable** (the design accepts *both*):
  - `rewrite=` — a `RewriteRule` list, a `"module:func"` string, or a callable →
    **live SYNC egress rewrite** (the `tls_write` hook). Equal-length substitution only
    (framing-safe); a length-changing rule is skipped in-agy and recorded (`rewrite_skip`).
  - `tools=` / `context=` — **additive**, via an injected MCP server (`config.py`).
- **`AgyResponse`**: `.text` (clean answer, prefers app→wire→transcript), `.source`,
  `.app_text`, `.turns` (all decoded `genai_turn`s), `.request` / `.events` / `.usage` /
  `.model` (substantive turn), plus the lazy diagnostic accessors `.rpc_trace` /
  `.stacks` / `.call_graph` / `.cgt_args` (each decodes its capture on demand — reading one
  never costs the others, and `.stacks` loads the funcmap only when touched). Also
  `.transcript`, `.exit_status`, `.instrumented` (+`.instrumented_reason`). `.stacks` needs
  `symbols/funcmap.tsv.gz` (`make symbols`); pass `funcmap=` to override it.
- **Introspect the capture surface**: `from pyagy import HOOKS, by_mech, enabled_hooks,
  by_kind, sync_capable, DERIVED_KINDS` — the machine-readable mirror of `src/procdef.h`
  (which hooks are installed, by mechanism, and which kinds rewrite egress).
- **Env knobs** (set for you by the kwargs above; also usable with `run-agy.sh`):

  | env var | set by | effect |
  |---|---|---|
  | `AGY_PROC_ENABLE` | (auto, instrumented) | the sole gate — opt the shim in; installs the full hook union |
  | `AGY_PROC_TLS_WRITE_SYNC` | `rewrite=` | make `tls_write` a SYNC (modify) hook |
  | `AGY_PROC_REWRITE_RULES` / `AGY_PROC_REWRITE` | `rewrite=` | rules-file path / `module:func` |
  | `AGY_PROC_STACK` | `stack=True` | emit deduped `callstack` events |
  | `AGY_PROC_CGT_ARGS` | `arg_probe=True` | emit `cgt_args` arg-graph reports |
  | `AGY_PROC_CONV_ID` | (auto, instrumented) | install the `os.OpenFile` probe → `conversation_id` event (exact id) |
  | `AGY_PROC_H2` / `AGY_PROC_CORRELATE` | — | `=0` disables HTTP/2 reassembly / the genai-turn correlator |

- **Always instrumented:** every launch goes through `AgyProcess` and loads the shim on the
  pinned `vendor/agy` (build-id-matched) — there is no clean/degrade path. `instrumented_env`
  puts `$CONDA_PREFIX/lib` on `LD_LIBRARY_PATH` so the `LD_PRELOAD` always resolves libpython;
  `AgyResponse.instrumented` is therefore always `True`. The shim + `vendor/agy` are a
  prerequisite (`make -C antigravity`).
- **Offline tests** (no agy/network/creds): `test_scripts/test_http1sse.py`,
  `test_config.py`, `test_client.py`, `test_appresponse.py`, `test_rpctrace.py`,
  `test_symbolize.py`, plus the `rewrite`/`config` offline suites. **Live** (skip cleanly
  per `test_trampoline.py`): `test_rewrite.py` round-trips a redaction on the wire;
  `test_trampoline.py` asserts the hook union completes a turn and decodes every surface; `test_agyprocess.py` drives
  agy as a `multiprocessing` child (round-trip + exception + stream + persistent);
  `test_agy_session.py` drives resume / list / continue + repo-scoped `data_dir` + pre-trust.

## Sessions: resume, list, and scope agy's native conversation store

agy persists every conversation to disk (`~/.gemini/antigravity-cli/conversations/<uuid>.db`
+ a readable `brain/<uuid>/…/transcript.jsonl`) and can resume one on a fresh launch. `pyagy`
surfaces that: **`Session` is the first-class object**, `ask()` is one-shot sugar over a
transient one, and both carry a resumable `conversation_id`.

```python
import pyagy
s = pyagy.Session()                      # live `agy --prompt-interactive`; in-run turns over the PTY
s.ask("Remember the code word BANANA.")
cid = s.conversation_id                   # captured id (agy has persisted the conversation on disk)
s.close()

# a fresh process, later — context restored from agy's own store:
pyagy.resume(cid).ask("What's the code word?")     # `agy … --conversation=<cid>`  -> BANANA
pyagy.continue_latest().ask("…")                    # `agy … --continue`  (most recent)
for c in pyagy.list_conversations(limit=10):        # id, title, step_count, last_modified
    print(c)
print(pyagy.read_transcript(cid))                    # stored turns (also s.history())
```

- **Turn model:** in-run turns ride one live `--prompt-interactive` process (as before); the
  durable part is **capturing** the `conversation_id` and **resuming** a stored conversation on
  a *new* launch via `--conversation=<id>` / `--continue` — both verified to recall context, in
  `--print` and interactive mode. `ask(..., conversation_id=…/continue_latest=True)` resumes in
  one-shot `--print`; `AgyResponse.conversation_id` is always the id the run created/continued.
- **How the id is captured:** instrumented runs read it *exactly, in-process* — agy doesn't put
  `ANTIGRAVITY_CONVERSATION_ID` in its own env (it's per-conversation, injected only into child
  processes), and it isn't in any RPC/`SendUserMessage` entry-arg graph, but agy opens
  `conversations/<uuid>.db` / `brain/<uuid>/…`, so the **`FILE_OPEN` hook** (`os.OpenFile`,
  an overlay enabled by `AGY_PROC_CONV_ID`) reads the uuid straight from the path and
  emits a `conversation_id` event. If a run's capture lacks that event, resolution falls back
  to newest-`*.db`-by-mtime. (`capture_conversation_id` prefers the event; verified event == mtime.)
- **`AgyProcess(conversation_id=…)`** (the multiprocessing child) and
  **`AgyModel(multi_turn=True | conversation_id=…)`** (the TaskSolver backend — then continues
  one conversation across calls, exposes `.session()`) are session-capable too.

**Workspace trust (default on).** agy blocks interactive startup on an *untrusted* workspace
with a "trust this folder" menu — the real cause of past interactive hangs. There is **no env
var and no trust flag** (only the blunt `--dangerously-skip-permissions`); the clean mechanism
is the config list `settings.json → trustedWorkspaces`. So every interactive launch
**pre-registers its workspace** there (`trust=True`, via `pyagy.trust_workspace`, atomic +
idempotent). Pass `trust=False` to opt out, or `skip_permissions=True` for the flag.

**Scope the data dir to a project repo (`data_dir=`).** Keep a project's conversations *with
the project* instead of the global `~/.gemini`:

```python
pyagy.Session(workspace=repo, data_dir=repo).ask("…")   # store at <repo>/.gemini/antigravity-cli/
```

agy hardcodes GeminiDir = `$HOME/.gemini` (`GEMINI_DIR`/`GEMINI_HOME` env are ignored) and
`--app_data_dir` is relative-only, so `data_dir=` scopes via an **HOME override** and seeds the
scoped tree with symlinks to the real login token + `config/` (so agy stays logged in — verified).
The global store is left untouched; `list_conversations(home=data_dir)` reads the scoped one.
Add the scoped `.gemini/` to `.gitignore`. `data_dir=None` (default) uses the global store.

## `AgyProcess`: the single agy launcher

`AgyProcess` is the one front-door every agy launch goes through (always instrumented, on the
pinned `vendor/agy`). It has two modes:

- **plain-CLI** (`target=None`, the default) — run agy as an external process and read its PTY
  transcript with `read_until_exit` (one-shot) / `read_until_idle` (interactive). Backs
  `session.run_print`, `session.InteractiveSession`, `pyagy.ask` / `pyagy.Session`, and the
  `AgySession` capture harness.
- **embedded-worker** (`target=callable`) — a `target` executes *inside* agy's embedded
  interpreter and streams **native Python objects** home over a `multiprocessing.connection`
  (no JSONL round-trip).

```python
from pyagy.agyprocess import AgyProcess
from pyagy.agy_process.mp_child import stream_turns, get_result_conn

# plain-CLI: run one --print turn, read the transcript (this is what pyagy.ask wraps)
p = AgyProcess(prompt="What is 2+2?"); p.start()
print(p.read_until_exit()); p.close()

# one-shot: stream agy's decoded model turns home as native dicts (the shim always
# installs the capture union, so genai_turn events flow)
p = AgyProcess(target=stream_turns, prompt="What is 2+2?"); p.start()
while p.poll(1.0) or p.is_alive():
    try: turn = p.recv()             # {"kind":"genai_turn","text":…,"usage":…,"request":…}
    except EOFError: break           # agy exited

# persistent, multi-turn (context retained)
p = AgyProcess(target=stream_turns, persistent=True, prompt="What is 2+2?"); p.start()
t1 = p.ask()                         # submit the prefilled prompt          -> "4"
t2 = p.ask("multiply that by 10")    # follow-up (agy remembers)            -> "40"

# arbitrary target: fn runs inside agy; send whatever you like home
def work(x): get_result_conn().send({"x": x})
p = AgyProcess(target=work, args=(41,)); p.start(); print(p.recv())
```

**How it works** (`pyagy/agyprocess.py` + `pyagy/agy_process/mp_child.py`):
- `AgyProcess(SpawnProcess)` + a custom `AgyPopen(popen_fork.Popen)` whose `_launch` execs agy
  under a PTY (`_pty.PtyProcess`) with the instrumented env. In **plain-CLI** mode that is all it
  does — the caller drives the PTY (`read_until_exit`/`read_until_idle`); there is no worker
  channel or pump thread. In **embedded-worker** mode (`target` set) it additionally hands the
  child a result `Pipe` + a boot pipe (both `os.set_inheritable`) via `AGY_MP_{MODE,CHAN_FD,BOOT_FD}`,
  pickles `(prep, process_obj)` under `set_spawning_popen`, and runs a pump thread.
  `start/join/exitcode/terminate` are inherited from `popen_fork.Popen` and track **agy's** pid;
  the *task result* flows over the Connection (agy owns lifetime, so completion is signalled on
  the channel, not by process death).
- Inside agy (the shim's worker imports `agy_process`, which starts a daemon thread when
  `AGY_MP_MODE=1`), the child runs the **real** `proc._bootstrap()` with three surgical
  neutralizations so it can't tear agy down: `sys.stdin=None` (defeats `util._close_stdin`,
  which would close the PTY's fd 0), `threading._shutdown`→no-op, and a trimmed `prepare()`
  (authkey/name only — no `sys.path=`/`chdir`/`_fixup_main`). `_bootstrap` is called directly,
  which skips `spawn_main`'s `sys.exit` and the `is_forking(argv)` assert.
- The PTY stays **parent-owned** (a pump thread answers agy's terminal-capability queries);
  persistent mode types prompts in and uses PTY-idle as the turn/ready boundary.
- **Python 3.13 both ends** (shim embeds pixi's `libpython3.13`), so parent↔child pickle is
  same-version and `pyagy` classes round-trip via the shared `PYTHONPATH`.

**WSL1:** the channel is `multiprocessing.connection` (`Pipe` — fd + pickle, **no semaphores**),
which works on WSL1; `Queue`/`Lock`/`SharedMemory` (POSIX `sem_open` → `EPERM` on the 4.4
kernel) are **cloud-only**. Runs the pinned `vendor/agy` (its build-id must match the shim).
Tests: `test_scripts/test_agyprocess.py`.

## Using agy as a TaskSolver backend (`pyagy`)

`antigravity/pyagy/` drives agy as a **model backend** with the same adapter
surface as TaskSolver's other providers (mirrors `ClaudeCodeModel`): it shells
out to `agy --print` under a PTY, in a throwaway git workspace, and parses the
reply. No API key (agy is logged in via `~/.gemini/antigravity-cli/`). This is
independent of the gum/LD_PRELOAD instrumentation — it treats agy as a black box.

```python
from tasksolver.common import TaskSpec, Question
from pyagy import AgyModel                       # on path in the pixi env
model = AgyModel(api_key=None, task=my_task, model="gemini-3-pro")
parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))
```

`import pyagy` works in the pixi env because the package ships it via
`[tool.setuptools.packages.find]` (the editable install's finder maps `pyagy` →
`antigravity/pyagy`; no `PYTHONPATH` needed). Smoke test: `pixi run python
test_scripts/example_agy_backend.py`. For multi-turn scripting use
`pyagy.InteractiveSession` (PTY + terminal-query responder).

**pixi/WSL1 note:** `pixi install` builds tasksolver as a conda package; on this
WSL1 host the setuptools link step needs the `wsl1-exec.so` shim (already in the
shell's `LD_PRELOAD`), otherwise it fails copying `_distutils_hack/__init__.py`.

## Risks / limitations

- **W^X / mprotect**: gum embedded needs `mprotect(PROT_EXEC|PROT_WRITE)` on `.text`.
  Validated on both WSL1 and a real cloud kernel (6.18.5) — see Status. The known WSL1
  0-byte-mmap bug is worked around on that host (see MEMORY); it does not apply to a real
  kernel.
- **Cloud-container caveats**: `make setup` and `pixi`/`agy` installers fetch from GitHub
  releases, which a locked-down egress policy may block (403) — vendor the frida-gum devkit
  (and, if needed, the pixi binary) from a mirror instead. agy sign-in works headlessly via
  its out-of-band OAuth code-paste flow (no in-container browser needed).
- **agy updates** change offsets and may reorder text again → re-run
  `build_symbols.py` (`make symbols`). Names are stable; the build-id guard blocks
  mismatched hooks.
- **SYNC + GC**: keep SYNC callbacks fast/CPU-bound (above).
- This instruments a binary **you run on your own machine** for research/interop.
  It does not defeat anyone else's security boundary.
