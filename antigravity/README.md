# antigravity — bind custom network / MCP context / tools into `agy`

`antigravity` instruments the **Antigravity CLI** (`antigravity/vendor/agy`, Google's agentic
coding tool, internal codename *jetski*) so we can observe and modify its
behaviour from our own **Python** code, in-process — without the source and
without a supported plugin API for what we need.

It does this by converting the upstream PIE `agy` binary into `antigravity/vendor/libagy.so`
(`make_libagy.py`: clears `DF_1_PIE`, sets `DT_SONAME = "libagy.so"`, exports
`runtime.rt0_go` as `agy_rt0_go`, `runtime.firstmoduledata` as `agy_firstmoduledata`,
and all 41 Go hook target functions as
`agy_go__<id>` in `.dynsym` — keyed by the hook row's id, so the Go side of a row reads as the
mirror of its host side `agy_stub__<id>` (a function two rows name is exported under both ids,
and the two cgocall gateways use the synthetic ids `CGOCALL`/`ASMCGOCALL`) — statically patches
each hooked Go function's `entry + skip` with a five-byte `E9 rel32` jump to its `.plt.agy`
entry, and imports all 28 `agy_stub__<id>` symbols via eager `R_X86_64_JUMP_SLOT` relocations
in `DT_JMPREL`, while retaining `PT_TLS` inside `libagy.so`) and
linking a small C++23 host executable `antigravity/vendor/agy` against `-lagy` as its first
`DT_NEEDED` dependency (so `ld-linux-x86-64.so.2` assigns `libagy.so` static TLS slot 1 at
`[%fs:-0x4a0 .. %fs:0)` with no external `agy_tls.S` needed). Each active row also exports
`agy_resume__<id>` at `entry + skip + ow` for the stub's continuation; the original entry
exports remain for introspection and patching. At startup, `main()` runs
`agy_init()` before calling `agy_rt0_go(argc, argv)`, combining two mechanisms:

1. **Bidirectional static `ld` / `ld.so` binding** (`-Wl,--export-dynamic`):
   - `vendor/agy` → `libagy.so`: direct ELF relocation binding (`R_X86_64_64` /
     `R_X86_64_GLOB_DAT`) of `agy_rt0_go`, `agy_firstmoduledata`, the `agy_go__*` gateways,
     and `agy_resume__*` continuation targets (plus libc `getaddrinfo` interposition for cgo DNS).
   - `libagy.so` → `vendor/agy`: `ld.so` resolves each `R_X86_64_JUMP_SLOT` into
     `.got.plt.agy` with the address of its static trampoline stub `agy_stub__<id>`.
     `BIND_NOW`/`NOW` resolve all slots at process load time; `.plt.agy` jumps through them
     without runtime code patching or `mprotect`.
2. **Static cgocall trampolines (`src/goframe.cpp` + `src/gocall.cpp` + `src/gomod.cpp`)**
   emitted at build time into the `agy_trampolines` section — named without a leading dot so the
   linker defines `__start_`/`__stop_` for it, which is where the region bounds come from — each stub is a `naked`
   function emitted by `AGY_STUB_FULLCGO` or `AGY_STUB_ASMCGO`, sharing save/restore text.
   `gen_sources.py` emits `AGY_STUBS(FULLCGO, ASMCGO)` with each stub's `id`, `book`, and `go_frame` into
   `src/symbols_gen.h`, preserving active `BOOKS` order — with a
   synthetic `moduledata` (`md->bad = 1`) spliced synchronously onto `agy_firstmoduledata.next`
   before `agy_rt0_go()` starts. `agy_enable_hooks` in `gocall.cpp` fills the zero-initialized
   `g_agy_on` handler pointer table for enabled rows; disabled entries remain `nullptr`.
   Each stub loads its entry once, skips the hook if null, and otherwise passes that pointer
   through the `ld`-bound `runtime.cgocall`/`runtime.asmcgocall` gateway to its C handler
   (`agy_on_fh_update`, `agy_on_stream`, `agy_on_file_open`,
   `agy_on_http_rt`, `agy_on_exit`, `agy_on_resp_chunk`, `agy_on_readlink_filter`,
   `agy_on_generic`). `resp` and `tls_write` share `agy_on_stream`; each event keeps the kind
   from its `BOOKS` row. `BOOKS` stores hook metadata only. frida-gum has been completely removed.

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
| Runs on **WSL1** (syscall-translation layer, not a real kernel) | `ptrace` injection is unreliable here. We statically patch `libagy.so` on disk and bind our `agy_trampolines` stubs through eager PLT relocations in `ld.so` — no `ptrace` and no runtime `mprotect`. |

**Build pinning.** Everything is pinned to the agy ELF BuildID (currently
`85d3425a18a809e545747dcb9d87c03c`, agy **1.4.11**). The shim refuses to install hooks if
the running binary's BuildID doesn't match `symbols.json`, so offsets can never be
silently applied to a different build. After updating the release pins, rebuild the library
and metadata with `pixi run shim-symbols`.

The 1.4.11 upgrade follows the package move of `createPlannerResponseStep` and
`(*ToolContextTrajectory).AppendStep` from `gemini_coder/framework/core` to
`oneharness/internal/core`; all 41 symbol targets resolve and pass prologue
verification. The rebuilt shim passes an instrumented `agy --version` startup check:
28/28 hooks install and Python receives smoke events.

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
        │   │ pyworker boost::thread (LARGE 16MB stack)   │  ← libpython3.15  │
        │   │  Py_InitializeEx(0); import pyagy.agy_process │    lives only     │
        │   │  loop: pop req → PyGILState_Ensure →        │    here (one      │
        │   │        call on_tls_write/read/http/tool →   │    PyThreadState) │
        │   │        write verdict → signal condvar       │                   │
        │   └───────────────────────────────────────────┘                   │
        └────────────────────────────────────────────────────────────────────┘
             ▲ vendor/agy links libagy.so (first DT_NEEDED) + wirecap_bridge;
               main() -> agy_init() (register moduledata, verify build-id, start pyworker, enable hooks) -> agy_rt0_go()
```

### Why a dedicated worker thread with a big stack

gum inline hooks fire **on whatever stack the hooked Go function was using — a
goroutine stack**, which starts ~8 KB and is *movable*. Calling libpython (deep C
stacks, arbitrary Python) directly there risks a stack overflow into the
goroutine's guard page (a hard crash — Go's `morestack` growth only triggers at
Go prologues, never inside our C code).

So the **goroutine-side hook body stays tiny** (copy the `[]byte`, push a
request) and all Python runs on a dedicated `boost::thread` with a 16 MB stack.
That thread is *not* a goroutine and is unknown to Go's scheduler, so Python's
deep stacks and the GIL never touch Go's runtime. (`pybridge.cpp` is C++23 and uses
Boost.Thread specifically because its `thread::attributes::set_stack_size` is the
only standard-ish way to set that 16 MB stack — `std::thread` has no stack-size API.)

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

Addresses come from generated `symbols/symbols.json` (`symbols.make_libagy` calls the
`symbols.build_symbols` resolver on the final ELF layout before patching, pinned to build-id).
Names are stable across Go versions;
offsets are not.

| Function | Go signature (register ABI) | Purpose | Mode |
| --- | --- | --- | --- |
| `crypto/tls.(*Conn).Write` | `(c *Conn=RAX, b.ptr=RBX, b.len=RCX, b.cap=RDI)` | plaintext **egress** to LLM backend | ASYNC (→SYNC for rewrite) |
| `crypto/tls.(*Conn).Read` | `(c=RAX, b.ptr=RBX, b.len=RCX)` → `n=RAX` on return | plaintext **ingress** from backend | ASYNC |
| `net/http.(*Transport).RoundTrip` | `(t=RAX, req *Request=RBX)` → `resp=RAX` | HTTP-level URL/headers (structured view) | ASYNC |
| *(candidates)* MCP/tool/prompt funcs | grep `symbols/funcmap.tsv.gz` | reserved for tool/context injection | — |

**Go register ABI (amd64, Go 1.17+).** Integer/pointer args go in
`RAX, RBX, RCX, RDI, RSI, R8, R9, R10, R11`; results in the same order. A `[]byte`
occupies three consecutive slots (ptr, len, cap). `R14` holds the goroutine `g`
pointer — **never clobber it**; gum saves/restores the full register set around
our callback, so reading is safe.

The TLS layer gives us HTTP/2 frames (HPACK-compressed headers, possibly gzipped
bodies, likely gRPC/protobuf). Reassembly to LLM request/response JSON happens
**in Python** (`wirecap/decode/h2reassemble.py`) — decoupled from agy internals, so it
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

**Reliability: gum is fully retired (2026-07).** A bisect traced intermittent model-turn failures
(long misread as backend rate-limiting) to frida-gum's inline self-modifying-code patching. Two
ways it corrupts agy's Go runtime: (1) `on_leave` return-value interception rewrites the return
address, which the GC stack-unwinder trips on — **~40% turn failure**; (2) even *entry* gum hooks
on hot funcs (`tls_write` ~55×/turn, `os.Getenv` ~116×/turn) fail the full AgyProcess path under
worker load (~15%). Both cgocall-trampoline variants are **100% across 55+ turns** where gum is
~60–85%. So **every hook is now a cgocall trampoline** — `AGY_ASMGO(symbol)` for hot non-parking funcs
(the lighter g0-switch: `os.Getenv`/`tls_write`) and `AGY_FULLGO(symbol)` for parking funcs (and for
`os.OpenFile`, which does an openat syscall and wants the P handoff)
— and capture is done via **entry-arg trampoline reads** (`agy_cgo_hook` reads args off the g0
stack: the `tls_write` request, the h2 `resp` chunk, the app boundary). The gum `leave=1` hooks
that read *return* values are `AGY_OFF` (`tls_decrypt`, the leaf getters, the R&D marshalers); the
decoded answer still arrives via the `fh_update` trampoline as `app_response`. The one capture that
was return-only — the wire `genai_turn` **response** (`tls_decrypt`) — is recaptured entry-arg on
the trampoline: `codeassistclient.toStreamResponseChunk(line string)` gets each raw SSE line
(`data: {"response": {...}}`) in `rax`/`rbx`, emitted as `resp_chunk` (and `sendUsageDelta` carries
model/usage). Reassembling those chunks into `genai_turn` is DONE — `BaseCorrelator.feed_chunk`
(`wirecap/decode/capture.py`) accumulates them and emits the turn at the terminal event.

**Approach A (`src/gocall.cpp` + `src/gomod.cpp`)** avoids gum's return-tracking
entirely. `make_libagy.py` statically patches the target — **past its stack-check prologue** —
with a five-byte `E9 rel32` jump to a per-hook `.plt.agy` entry. The converter creates each
eight-byte entry (`FF 25 <disp32>` plus `66 90`) and an eight-byte RW `.got.plt.agy` slot.
`ld.so` fills the slot through `R_X86_64_JUMP_SLOT` in `DT_JMPREL`, with `BIND_NOW`/`NOW`
requiring resolution before Go starts. Eager binding is required: the lazy resolver would run
on the Go stack and can clobber live Go argument registers such as R10/R11. The entry patch
is one byte shorter, with one additional jump in the hook path. The PLT entry reaches a
static assembly trampoline stub
(`agy_stub__<id>` in `agy_trampolines`) that:

1. loads `g_agy_on[book]` into `r12`; a null entry skips straight to the displaced prologue,
2. snapshots the Go-ABI arg registers into a stack **block**,
3. passes the same handler pointer from `r12` to the row's Go→C gateway
   with `fn=handler, arg=&block`. `runtime.cgocall` switches to the g0/system stack and
   provides the safe cgo context (`entersyscall`, P-handoff, GC coordination);
   `runtime.asmcgocall` is used for the non-parking rows. The gateway runs the handler
   directly; the handler copies + enqueues to the pyworker,
4. zeros X15, restores the registers, and returns if the handler set `block.action`; otherwise it runs the
   displaced original frame-setup prologue and resumes with
   `jmp *agy_resume__<id>@GOTPCREL(%rip)`. The alias already points at `entry + skip + ow`.

Generated entries have the form `FULLCGO(id, book, go_frame)` or `ASMCGO(id, book, go_frame)`,
for example `ASMCGO(smoke, 0, 0x10)`. `go_frame` is the original Go function's stack allocation.
After releasing the fixed 112-byte trampoline frame, the shared basic asm reconstructs
`pushq %rbp; movq %rsp, %rbp; subq $go_frame, %rsp`. The generator accepts only the known
8/11-byte prologues with canonical nonnegative immediates; native tests check the exact emitted bytes.

Because the trampoline never rewrites *our* return address, parking/rescheduling is
unaffected — the goroutine may resume on any M. Each `procdef.h` row has the form
`{id, mode, retcap, selector(symbol)}`: `AGY_FULLGO(symbol)` selects full `runtime.cgocall`
(the robust default — `entersyscall` + P handoff, GC-safe); `AGY_ASMGO(symbol)` selects
`runtime.asmcgocall`, a lighter variant (just the g0 stack switch, no `entersyscall`/`_Gsyscall`)
for hot or syscall-at-entry-sensitive funcs; `AGY_OFF(symbol)` disables the hook. C++ keeps
the wrapper's mechanism enum in `BOOKS` and discards the symbol; the Python parser reads both.
Each stub variant names its gateway directly. Both share one region and synthetic moduledata,
with a separate pcsp sequence for each variant.

**Synthetic moduledata (`gomod.cpp`).** `cgocall`'s `entersyscall()` opens a GC-scannable window
over our trampoline frame; if Go's unwinder can't `findfunc` the trampoline PCs it
`throw("unknown pc")`s. `agy_gomod_register()` installs a **synthetic moduledata** (with
`md->bad = 1`, so `runtime.modulesinit` skips it while `runtime.moduledataverify1` and
`runtime.findfunc` validate and walk it) spliced synchronously onto the `ld`-bound
`agy_firstmoduledata.next` before `agy_rt0_go()` starts. Its pclntab/pcsp cover the trampoline,
so GC unwinds it cleanly — and the pcsp must report **one constant spdelta** across the whole
trampoline-frame window. `gomod.cpp` emits the two sequences once, then selects each `_func`'s
pcsp offset while walking active `BOOKS` rows in stub order. Both use a 112-byte frame and
256-byte slots; the live frame windows are `[20,167)` for FULLCGO and `[20,173)` for ASMCGO,
ending after their respective stack-restore instructions (`goframe.h`).

**Fixed frame layout.** `runtime.cgocall` spills its two register args (`fn`, `arg`)
into caller-provided slots at `[S]` and `[S+8]` (`S` = rsp at the call). The block starts at
`S+16`, after those 16 reserved bytes. FULLCGO passes `fn`/`arg` in RAX/RBX and leaves the
spill pair for `cgocall`; ASMCGO writes `fn` from R12 and `arg` from RAX into those stack slots.
The block's `uint32_t book` and `uint32_t action` share the
first qword, followed by the 11 saved registers: `AGY_FRAME = 16 + 96 = 112` bytes.
`movq $book, 16(%rsp)` initializes the row index and clears `action` together. The pinned
`runtime.asmcgocall` writes its 32-bit return value at `S+16` after the handler finishes;
this overwrites the now-unused `book` while leaving `action` at `S+20` intact.

The frame stays fixed through the Go gateway so the real stack delta matches the synthetic
pcsp throughout the `_Gsyscall` window. On return, `cmpl` tests `action` while the frame is
live and `lea` restores `rsp` without changing the branch flags; the pcsp delta changes
after the instruction ends. Synthetic functions carry `FuncFlagAsm` so Go cannot
asynchronously preempt a partial register save/restore or the displaced prologue.
Synchronous gateway stack walks still use the fixed frame's pcsp. X15 is zero on Go entry;
the stub only touches integer registers before the gateway and zeros X15 immediately
afterward, covering both the normal continuation and early return.

The `AGY_FULLGO`/`AGY_ASMGO` rows in `procdef.h` generate the static stubs, and
`agy_enable_hooks` selects their handlers before Go starts. `os.Getenv` remains enabled
as the `smoke` ASMCGO hook used by the native startup checks. The synthetic moduledata
covers the whole trampoline region.

### App-boundary text probe — why the wire (`http1sse`) wins for the response

We evaluated hooking agy's **app boundary** to get the model response as pre-decoded Go
values instead of parsing HTTP/1.1+SSE. The `AGY_PROC_CGT_ARGS` diagnostic (a fault-safe
`process_vm_readv` recursive object-graph string-finder in `gocall.cpp`, gated on that env; the
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
every hook fire. `agy_backtrace` (gocall.cpp) walks the frame-pointer chain (`[rbp]`=saved rbp,
`[rbp+8]`=return addr — Go keeps frame pointers) with **fault-safe `process_vm_readv`** reads,
emitting each return PC as a link vaddr (`pc - base`). `build_symbols.py` writes a full sorted
`symbols/funcmap.tsv.gz` (addr→name over all ~132k funcs; gitignored, regenerated by
`pixi run shim-symbols`), and `pyagy/agy_process/symbolize.py` bisects it to render named stacks + an
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
funcmap (`pixi run shim-symbols`); when it's absent the accessor returns a short reason string.

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
  CMakeLists.txt            ← the build: generates symbols_gen.h + links vendor/agy against vendor/libagy.so
  build/                    ← CMake files + immutable raw agy cache, keyed by archive SHA512
  vendor/                   ← libagy.so (164 MB) + built vendor/agy host executable
  symbols/                  ← source build package; run with `python3 -m symbols.<tool>` from antigravity/
    __init__.py
    agyelf.py               ← shared parsing: procdef.h rows and hook targets, ELF (lief) + Go pclntab/moduledata,
                              prologue skip/extract. The three tools below cannot disagree about
                              an offset. All three use lief from the build environment.
    build_symbols.py        ← resolver used before patching: pclntab + moduledata.text →
                              symbols.json + funcmap; verifies every installed hook's prologue
    make_libagy.py          ← converts PIE agy → libagy.so; exports agy_go__* / agy_resume__*,
                              creates .plt.agy / .got.plt.agy with eager JUMP_SLOT imports,
                              captures metadata, then patches entry+skip with E9 rel32.
                              `--update` queries the latest release manifest and prints new pins;
                              `--wsl1` applies the tcmalloc MAP_FIXED_NOREPLACE fix on its own (a
                              conversion applies it automatically when running on WSL1).
    gen_sources.py          ← symbols.json + procdef.h → symbols_gen.h (build id + AGY_STUBS(FULLCGO, ASMCGO),
                              one (id, book, go_frame) entry per installed hook); `--hooks` →
                              pyagy/agy_process/hooks.py
    symbols.json            ← generated, gitignored: {build_id, text_base, hooks:{name→vaddr}, skips, prologues}
  src/
    goframe.h               ← the stub frame layout, shared by goframe.cpp and the rest of the
                              C++ (gomod.cpp static_asserts every offset against agy_block)
    goframe.cpp             ← AGY_STUB_FULLCGO / AGY_STUB_ASMCGO emit each `naked` trampoline
                              with shared basic-asm save/restore text; compiled -fno-lto
    antigravity.cpp         ← main() entrypoint + agy_init() + getaddrinfo interposer
    gomod.cpp / gocall.cpp  ← synthetic Go moduledata + handler-table setup, C handlers & stack unwinder
    pybridge.cpp/.h         ← embed libpython (C++23); pyworker boost::thread, Boost.Python
                              object/call layer, queue, dispatch
    procdef.h               ← BOOKS metadata + consteval bk(): { id, mode, retcap, selector(symbol) }
  pyagy/                    ← the `pyagy` Python package (importable; ships in the pkg)
    __init__.py             ← lazy exports (PEP 562): ask/Session/AgyResponse/specs,
                              AgyModel, write_mcp_config
    client.py               ← public API: ask() (one-shot) + Session (multi-turn) +
                              AgyResponse + ToolSpec/ContextResource/RewriteRule/Usage
    config.py               ← MCP config-injection (write_mcp_config/validate_server)
    model.py                ← AgyModel (prepare_payload/ask/rough_guess/run_once/…) over client.ask
    agyprocess.py           ← AgyProcess: the SpawnProcess handle (lifecycle + PTY passthroughs +
                              argv assembly) — THE single agy front-door; a Process, not the queue's
                              owner. Runs an in-agy worker that streams the decoded answer home over
                              the caller-owned SimpleQueue (client.py owns collect/ask/collect_many)
    _pty.py                 ← PtyPopen(popen_fork.Popen): AgyProcess's `_Popen` — execs agy under
                              a PTY as the mp child, owns the PTY (fork/read/answer) + the
                              launch/lifecycle; always instrumented on the pinned vendor/agy
    _term.py / _env.py      ← ANSI+terminal-query responder / instrumented-env wiring
    agy_process/__init__.py ← dispatch() → on_tls_write/on_tls_read/on_http/on_dns/on_smoke
    agy_process/http1sse.py ← HTTP/1.1 + SSE decoder for the MODEL endpoint (the right
                              one — the model turn is not HTTP/2)
    agy_process/rewrite.py  ← SYNC egress rewrite registry (equal-length, mtime reload)
    agy_process/hooks.py    ← machine-readable mirror of procdef.h
    (the correlator, the JSONL recorder, the HTTP/2+HPACK reassembler, the mp-child runner
     and the HTTP/1.1+SSE framing all live in the SHARED ../../wirecap/decode/ — pycodex
     consumes the same ones; agy_process/http1sse.py is the genai-specific layer on top)
    agy_mcp_server.py       ← stdio MCP server (built-in stub or AGY_MCP_SPEC-driven)
  config/
    mcp.json  agents.json  README.md  ← native custom MCP server / agent (hybrid path)
```

The in-process module is **`pyagy.agy_process`** (loaded by `vendor/agy` via
`WIRE_MODULE`, which defaults to it). Test/experiment drivers live in the repo's
`test_scripts/`: `run-agy.sh`, `agy_session.py`, `analyze_capture.py`, `example_agy_backend.py`.

## Build & run

`pixi install` **builds `vendor/libagy.so` and `vendor/agy`** — the tasksolver package build (`setup.py` →
`build_py`) runs `cmake -S antigravity -B antigravity/build -G Ninja && cmake --build …`,
which caches the pinned upstream raw `agy` at configure time after verifying the tarball's SHA512.
The build converts it via `python3 -m symbols.make_libagy` into `antigravity/vendor/libagy.so`
and generates its metadata, then compiles the linked host executable `antigravity/vendor/agy`.
Since the build is x86-64-specific and a main
target, the package is **arch-specific (linux-64)** (`[tool.pixi.package.build.config]
noarch=false`) and the build is **required, with no skip/opt-out** — it fails loudly
if the toolchain (g++ / libpython / lief) is missing, because a wheel without the
shim can't run the `agy` backend. (`cmake`, `ninja`, `hpack`, and `brotli-python`
are pixi/conda deps — no `pip install` needed.)

The raw cache is `build/antigravity-<archive-sha512>.raw`; a new pin gets a separate cache entry.
Every conversion starts from this unchanged input. After LIEF writes the final ELF layout,
the converter records original prologues and addresses in `symbols.json` and `funcmap.tsv.gz`,
then writes each five-byte hook jump and publishes the library and metadata together.
Original prologue bytes from offset 5 onward stay intact; the host replays the complete
saved 8/11-byte prologue before jumping to its continuation alias.
Both metadata files are generated and gitignored. `-DAGY_BIN=/path/to/raw-agy` selects an
explicit raw input instead of the download; `vendor/agy` is the compiled host, never an input.

pixi caches the package build, so **after an agy update** rebuild the shim explicitly via
the pixi tasks:

```bash
pixi run shim-agy-update  # query the latest release manifest and print new CMakeLists pins
pixi run shim-symbols     # ensure library + metadata are current, then recompile
pixi run shim-hooks       # after editing src/procdef.h: regen pyagy's BOOKS mirror
pixi run build-shim       # rebuild changed outputs (fetches pinned raw agy on first run)
```

The tools in `antigravity/symbols/` form a source package and share code through
relative imports (`from .agyelf import ...`). Run them as modules from `antigravity/`;
CMake and the `shim-hooks` Pixi task set that working directory explicitly, including
during a build before TaskSolver is installed. All three tools require LIEF, supplied
by the Pixi default and build host environments. The `symbols` package is not installed
as part of the runtime wheel.

For direct use inside an activated Pixi environment:

```bash
cd antigravity
python3 -m symbols.make_libagy /path/to/raw-agy vendor/libagy.so symbols/symbols.json
python3 -m symbols.gen_sources --hooks
python3 -m symbols.gen_sources symbols/symbols.json src/symbols_gen.h
```

To check metadata validation, stub generation, the table parser, and the checked-in Python mirror,
run from `antigravity/`:

```bash
pixi run python -m unittest symbols.test_procdef symbols.test_build_symbols symbols.test_gen_sources symbols.test_make_libagy
```

Bumping agy is `shim-agy-update` (it prints the `AGY_VERSION`/`AGY_URL`/`AGY_SHA512` lines to paste
into `CMakeLists.txt`) followed by `shim-symbols`. Commit the updated pins; the library and
metadata are regenerated from the verified raw release.

**libpython / Python version.** The shim embeds the **pixi env's libpython 3.15**
(`$(CONDA_PREFIX)/bin/python3-config`), unified with the pixi-3.15 driver so the
multiprocessing/pickle wire between them is same-version (see *AgyProcess* below). It links
`-Wl,-rpath,$(CONDA_PREFIX)/lib` so agy finds `libpython3.15.so.1.0` at runtime (agy is always
launched via `pyagy/_env.py` under the pixi env). Build with `pixi run build-shim`
(needs `g++` + `/usr/include/linux/limits.h` from the conda kernel-headers dep). The host and the bridge
build with GCC LTO (`-DTASKSOLVER_LTO=OFF` disables it). The `g_agy_on` handler table carries
explicit `used` and `externally_visible` attributes because LTO cannot see its asm references.
`agy_enable_hooks` selects handlers once before Go starts; `nullptr` disables a row,
including `file_open` when conversation-id capture is off. The stub translation unit stays
`-fno-lto` and uses basic asm: a single load from `g_agy_on+8*book` into `r12`, a null check,
then that same pointer passed through the Go gateway.

After stub or toolchain changes, run `pixi run python test_scripts/test_agy_stubs.py` to check
frame geometry, the nullable gate, the PLT/GOT targets and eager loader binding, and instrumented
startup with disabled hooks and the conversation-id toggle. These checks require built native
artifacts and make no model calls.

`pyagy.agy_process` needs `hpack`/`brotli` only for the
HTTP/2 body decode (both are guarded/lazy), so it imports fine without them.

Run agy under the hook via the launcher in `test_scripts/` — it enables the full working
hook union (all 28 static `agy_trampolines` stubs bound via `ld` / `ld.so`, each hook's cgo
gateway chosen by the `procdef.h` target wrapper); the GC-safe synthetic moduledata is
always on:

```bash
pixi run test_scripts/run-agy.sh --version  # instrumented startup; no model call
test_scripts/run-agy.sh <normal agy args...>   # capture request+response+app+rpc (authenticated agy)
python3 test_scripts/analyze_capture.py agy-capture.jsonl --plot traffic.png
```

The startup check writes `antigravity.log` and `agy-capture.jsonl` in the current
directory (override with `AGY_PROC_LOG` and `WIRE_CAPTURE`). Check the native log for
the matching BuildID and installed hooks; a successful `--version` alone does not
test model traffic. The status investigations below describe older releases; the
current 1.4.11 validation scope is recorded under **Build pinning** above.

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
  (the historical `frame=120` layout) prepared, trampoline installed. The `mprotect(PROT_EXEC|PROT_WRITE)` W^X
  concern did **not** materialize here.
- ✅ **Live-turn matrix** (`agy --print` under a PTY): stage 9 (`os.Getenv`, 234 trampoline
  transitions) and stage 8 (`SendUserMessage`=1 + `callbackStreamer.Send`≈20) both **complete
  the turn** — cgocall == asmcgocall == no-hook baseline, with zero `throw`/`unknown pc`/panic/
  `UnicodeDecodeError` and zero `$HOME is not defined`.
- ✅ **gdb direct root-cause proof** (impossible on WSL1, where gdb couldn't run): breaking
  inside `runtime.cgocall` for our invocation shows its prologue spilling `fn→[S]`, `arg→[S+8]`
  (`mov %rax,0x38(%rsp)`/`mov %rbx,0x40(%rsp)`); with the fix `&block − S == 16`, so after the
  spill the block's first word (then the `"cgt_getenv"` tag pointer) and `block.regs.rax` are byte-for-byte intact — the two
  spill slots land entirely in the 16-byte scratch area. Pre-fix (`&block==S`) they'd be
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
why we hook decrypt, not read. `WIRE_PREVIEW` (legacy `AGY_PROC_PREVIEW`) sets capture bytes/event.

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
  `symbols/funcmap.tsv.gz` (`pixi run shim-symbols`); pass `funcmap=` to override it.
- **Introspect the capture surface**: `from pyagy import BOOKS, by_mech, enabled_hooks,
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
  | `AGY_PROC_H2` / `WIRE_CORRELATE` | — | `=0` disables HTTP/2 reassembly / the genai-turn correlator. The correlator + capture + preview knobs configure SHARED `wirecap.decode` objects, so they take the neutral `WIRE_*` names pycodex also uses (legacy `AGY_PROC_*` still accepted); H2 reassembly is agy-only and keeps its name |

- **Always instrumented:** every launch goes through `AgyProcess` and loads the shim on the
  pinned `vendor/agy` (build-id-matched) — there is no clean/degrade path. The shim is injected
  by running agy through its own `PT_INTERP` with the loader's `--preload` (`_env.preload_argv`),
  **not** `LD_PRELOAD`: an env var is inherited by every process agy spawns (a Playwright/Node
  helper, each `bash -c` tool command), each of which would then needlessly load the shim. The
  loader's `--library-path` points at `$CONDA_PREFIX/lib` so the shim's libpython resolves, so no
  `LD_PRELOAD`/`LD_LIBRARY_PATH` is left in the environment at all. `AgyResponse.instrumented` is
  therefore always `True`. The shim + `vendor/agy` are a prerequisite (`pixi run build-shim`).
- **Offline tests** (no agy/network/creds): `test_scripts/test_http1sse.py`,
  `test_config.py`, `test_client.py`, `test_appresponse.py`, `test_rpctrace.py`,
  `test_symbolize.py`, plus the `rewrite`/`config` offline suites. **Live** (skip cleanly
  per `test_trampoline.py`): `test_rewrite.py` round-trips a redaction on the wire;
  `test_trampoline.py` asserts the hook union completes a turn and decodes every surface; `test_agyprocess.py` drives
  agy as a `multiprocessing` child (round-trip + exception + stream + persistent);
  `test_agy_session.py` drives resume / list / continue + repo-scoped `data_dir` + pre-trust.

## Sessions: resume, list, and scope agy's native conversation store

> The Session lifecycle (lazy start, dead-process guard, the persistent turn
> loop) lives on the shared `wirecap.runtime.session.WireSession` base since
> the pykimi port — `pyagy.Session` keeps its full kwargs surface and supplies
> only agy's process construction and `AgyResponse` shape.

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
  `conversations/<uuid>.db` / `brain/<uuid>/…`, so the **`file_open` hook** (`os.OpenFile`,
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
pinned `vendor/agy`). Modeled on stock `multiprocessing.Process`, it does **not** own the result
channel: the **caller** creates the result `SimpleQueue` and passes it as a target arg —
`AgyProcess(target=stream_turns, args=(q,))`, exactly like `Process(target=f, args=(q,))` — so it
rides the process pickle. It always runs an in-agy worker `target`:

- **default target** (`stream_turns`) — receives the queue as arg0 and streams agy's decoded answer
  (`genai_turn` + `app_response`) home over it; the caller (`pyagy/client.py`) drains it with the
  module helpers `_collect` (one-shot) / `_ask_turn` (interactive) / `_collect_many` (parallel),
  which use `AgyProcess.service_pty` + `q.get()`. Backs `pyagy.ask` / `pyagy.Session`, `AgyModel`,
  and the `AgySession` capture harness. The PTY transcript is drained as a diagnostic byproduct
  (`.transcript`).
- **custom target** (`target=callable`) — your callable executes *inside* agy's embedded
  interpreter, receiving the queue as arg0 (or via `get_result_conn()`), and `.put`s **native
  Python objects** home over it (no JSONL round-trip).

```python
from multiprocessing import get_context
from pyagy.agyprocess import AgyProcess
from wirecap.decode.mp_child import stream_turns, get_result_conn

# the caller owns the queue and drains it (client.py's _collect/_ask_turn wrap this)
q = get_context("spawn").SimpleQueue()
p = AgyProcess(target=stream_turns, args=(q,), prompt="What is 2+2?")
p.start(); q._writer.close()          # writer close → reader EOFs on agy death (crash detection)
while p.service_pty(1.0, [q._reader]): # drains the PTY in the same wait it reads the queue
    o = q.get()                        # {"kind":"genai_turn",…} or ("_agy_done", code) on completion
    if isinstance(o, tuple): break

# arbitrary target: fn runs inside agy (conn = args[0]); .put whatever you like home
def work(conn, x): conn.put({"x": x})
q = get_context("spawn").SimpleQueue()
p = AgyProcess(target=work, args=(q, 41)); p.start(); q._writer.close(); print(q.get())
```

**How it works** (`pyagy/agyprocess.py` + `pyagy/_pty.py` + `wirecap/runtime/{process,pty}.py` + `wirecap/decode/mp_child.py`):
- `AgyProcess(SpawnProcess)` + a custom `_pty.PtyPopen` shaped like `popen_spawn_posix.Popen`:
  `_launch` execs agy under a PTY with the instrumented env — PtyPopen owns the PTY
  (fork/read/answer), the process lifecycle, and child fd inheritance (`resource_tracker` fd +
  `duplicate_for_child`), but **not** the result queue. The boot payload (one `WIRE_MP_BOOT_FD`
  pipe) is `tracker_fd` then `(prep, process_obj)` pickled under `set_spawning_popen`; the caller's
  `SimpleQueue` rides `process_obj`'s args, so `duplicate_for_child` makes its pipe fds inheritable
  and its SemLocks pickle by name. There is no pump thread — the caller drains the PTY via
  `AgyProcess.service_pty(timeout, [q._reader])` (a passthrough to `PtyPopen._service`) in the same
  `_conn.wait` it uses to read the queue. `start/join/exitcode/terminate` are inherited and track
  **agy's** pid; the *task result* flows over the caller's queue (agy owns lifetime, so completion
  is signalled on the channel via `("_agy_done", code)`, not by process death), and the
  ANSI-stripped PTY transcript is kept on `transcript` as a diagnostic byproduct.
- Inside agy (the shim's worker imports `agy_process`, which starts a daemon thread when
  `WIRE_MP_BOOT_FD` is set), the child reads `tracker_fd` and re-attaches to the parent's
  `resource_tracker` (so the queue's SemLocks share it) **before** unpickling `process_obj`, pulls
  the queue out of `proc._args[0]` (dropping the inherited reader), then runs the **real**
  `proc._bootstrap()` with three surgical neutralizations so it can't tear agy down:
  `sys.stdin=None` (defeats `util._close_stdin`, which would close the PTY's fd 0),
  `threading._shutdown`→no-op, and a trimmed `prepare()` (authkey/name only — no
  `sys.path=`/`chdir`/`_fixup_main`). `_bootstrap` is called directly, which skips `spawn_main`'s
  `sys.exit` and the `is_forking(argv)` assert.
- The PTY stays **parent-owned** (`_service` answers agy's terminal-capability queries as it
  drains); persistent mode types prompts in and uses PTY-idle as the turn/ready boundary.
- **Python 3.15 both ends** (shim embeds pixi's `libpython3.15`), so parent↔child pickle is
  same-version and `pyagy` classes round-trip via the shared `PYTHONPATH`.

**Channel:** a spawn `SimpleQueue` — a `Pipe` (fd + pickle) plus two named `SemLock`s (POSIX
`sem_open`) re-attached to the shared `resource_tracker` — created by the caller and handed to the
worker via `args`. The put side is a single serialized writer (the daemon thread's one sender loop),
so there is no feeder thread racing agy's neutralized shutdown. Because it uses POSIX semaphores it
needs a kernel where `sem_open` works (cloud). Runs the pinned `vendor/agy` (its build-id must match
the shim). Tests: `test_scripts/test_agyprocess.py`.

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
`pyagy.Session` (the first-class multi-turn object).

Images reach agy as PNG files (inside the workspace when one is set) named by
absolute path; the prompt tells the agent to open them with `view_file`, agy's
file tool (it has no `Read` tool).

### Chat mode (`AgyModel(chat=True)`)

For using agy like a chat-completion call (e.g. as a judge on a subscription
login), `chat=True` writes a workspace agent,
`<workspace>/.agents/agents/tasksolver-chat/agent.md`, and runs every call with
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

With agy 1.2.11 the answer turns then declare `view_file` and `manage_task` (a
harness tool with nothing to manage) and carry a ~220-character system prompt
instead of agy's ~8.7k-token default; agy 1.2.10 and earlier ignore workspace
agents under `--print`. A call that uses any other tool, or opens a file it was
not handed, raises `ChatModeViolation`. The stream-json `init` event lists all
57 registered tools whatever the agent — it is not what the model is offered.
Chat mode also runs with `trust=False` (plain agy does not need the workspace in
the global `trustedWorkspaces`; `trust=True` adds every scratch workspace to it)
and without `SSH_CLIENT`/`SSH_CONNECTION`/`SSH_TTY`, which make agy skip its
keyring login. Each call still makes agy's own session-title request, a small
tool-less side call on a flash-lite model.

**pixi/WSL1 note:** `pixi install` builds tasksolver as a conda package; on this
WSL1 host the setuptools link step needs the `wsl1-exec.so` shim (already in the
shell's `LD_PRELOAD`), otherwise it fails copying `_distutils_hack/__init__.py`.

## Risks / limitations

- **W^X / mprotect**: gum embedded needs `mprotect(PROT_EXEC|PROT_WRITE)` on `.text`.
  Validated on both WSL1 and a real cloud kernel (6.18.5) — see Status. The known WSL1
  0-byte-mmap bug is worked around on that host (see MEMORY); it does not apply to a real
  kernel.
- **Cloud-container caveats**: `cmake configure` downloads the pinned agy and the `pixi`
  installer fetches from GitHub releases; a locked-down egress policy may block either (403) —
  point `-DAGY_BIN=` at a local raw copy of the pinned agy (and fetch the pixi binary from a mirror)
  instead. agy sign-in works headlessly via
  its out-of-band OAuth code-paste flow (no in-container browser needed).
- **agy updates** change offsets and may reorder text again → update the release pins and run
  `pixi run shim-symbols` to regenerate the library and metadata. Names are stable; the build-id guard blocks
  mismatched hooks.
- **SYNC + GC**: keep SYNC callbacks fast/CPU-bound (above).
- This instruments a binary **you run on your own machine** for research/interop.
  It does not defeat anyone else's security boundary.
