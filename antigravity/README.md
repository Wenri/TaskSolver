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
dedicated worker thread, where your logic lives (`antigravity/python/agy_hooks/`).

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

**Build pinning.** Everything is pinned to the agy ELF BuildID
`4368698a979e6df1c84d2af6ffe16020`. The shim refuses to install hooks if the
running binary's BuildID doesn't match `symbols.json`, so offsets can never be
silently applied to a different build. Re-run the extractor after any `agy`
update.

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
        │   │ pyworker pthread  (LARGE 16MB stack)        │  ← libpython3.12  │
        │   │  Py_InitializeEx(0); import agy_hooks       │    lives only     │
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
**in Python** (`agy_hooks/h2reassemble.py`) — decoupled from agy internals, so it
survives agy updates.

---

## Hybrid tools / mcp_context

`agy` is an MCP *client*, so the robust way to add custom **tools** and
**mcp_context** is to register a custom MCP server via `config/mcp.json` (it can
be backed by TaskSolver). Hooks are the fallback for things config can't do
(e.g. injecting context invisibly into prompt assembly, or overriding a tool
result). See `config/` and task #6.

---

## Layout

```
antigravity/
  README.md                 ← this file (the design)
  setup.sh                  ← fetch frida-gum devkit + vendor UAPI headers (deps not committed)
  run-agy.sh                ← launcher: sets LD_PRELOAD, PYTHONPATH, AGY_HOOK_*, GODEBUG
  vendor/agy                ← copied-in binary, gitignored (164 MB)
  symbols/
    build_symbols.py        ← authoritative resolver: pclntab + moduledata.text →
                              symbols.json; self-verifies every hook is a prologue
    symbols.json            ← {build_id, text_base, hooks:{name→vaddr}, catalog}
  native/
    antigravity.c               ← LD_PRELOAD constructor + gum install + Go-ABI hook
                              callbacks + getaddrinfo interposer
    pybridge.c/.h           ← embed libpython, pyworker thread, queue, dispatch
    hooks.def               ← declarative hook table (id, symbol, mode, kind, stage, leave)
    gen_symbols_header.py   ← symbols.json → symbols_gen.h (build-id + name→vaddr)
    build.sh / Makefile     ← build antigravity.so (build.sh = no-make path)
    vendor/                 ← frida-gum devkit + UAPI headers (gitignored; ./setup.sh)
  python/
    agy_hooks/__init__.py   ← dispatch() → on_tls_write/on_tls_read/on_http/on_dns/on_smoke
    agy_hooks/record.py     ← JSONL recorder for plotting
    agy_hooks/h2reassemble.py ← HTTP/2 + HPACK + gzip stream reassembly
    agy/                    ← TaskSolver-contract backend (drive agy as a model):
      model.py  → AgyModel (prepare_payload/ask/rough_guess/run_once/…)
      session.py→ run_print (one-shot) + InteractiveSession (multi-turn PTY)
    agy_session.py          ← hook-integrated interactive driver (capture experiments)
    agy_mcp_server.py       ← minimal stdio MCP server (hybrid native tools/context)
    analyze_capture.py      ← summarize / plot a capture
    example_agy_backend.py  ← smoke test for the AgyModel backend
  config/
    mcp.json  agents.json  README.md  ← native custom MCP server / agent (hybrid path)
```

## Build & run

```bash
cd antigravity
./setup.sh                                             # fetch frida-gum + UAPI headers (once)
python3 symbols/build_symbols.py vendor/agy symbols/symbols.json   # after any agy update
./native/build.sh                                      # or: make -C native

# Incremental bring-up (AGY_HOOK_STAGE): 1=python only, 2=smoke hook, 3=real hooks.
AGY_HOOK_STAGE=2 ./run-agy.sh --help                   # smoke: os.Getenv → capture
AGY_HOOK_STAGE=3 ./run-agy.sh <normal agy args...>     # capture tls/http (fires under an
                                                       # authenticated agy session)

python3 python/analyze_capture.py agy-capture.jsonl --plot traffic.png
pip install hpack          # optional: decode HTTP/2 request/response headers
```

## Status (validated on this WSL1 host, agy build 4368698a…)

- ✅ libpython embeds in-process; worker thread runs; `agy_hooks` imports.
- ✅ frida-gum **embedded** inline hooking works on WSL1 (no ptrace).
- ✅ Hook fires on a real Go function (`os.Getenv`), reads register-ABI string
  args, marshals to Python, records to JSONL — agy runs to completion, no crash.
- ✅ `on_leave` (return-address rewrite, used by `tls_read`) is safe on ordinary
  Go funcs (100+ calls, no `unexpected return pc`).
- ✅ **Interactive session driving works** (`python/agy_session.py`): under a PTY
  (with a terminal-query responder) we ran a real multi-turn session against the
  logged-in agy — `"what is 2+2"` → `4`, then `"×10"` → `40` — reading agy's
  rendered output and injecting follow-up input. This is stage 1 (no network hooks).
- ✅ **In-process model-request capture works** (`AGY_HOOK_STAGE=3`). Hooking
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

## Capturing model traffic (given stage 3 is out)

**In-process request capture works** (`AGY_HOOK_STAGE=3`). Two hazards had to be
solved, both validated on full turns that reach `daily-cloudcode-pa.googleapis.com`:

1. **morestack re-entry.** Hooking *at* a func entry: when the frame grows the
   stack, Go's `morestack` re-runs the entry and re-enters our trampoline →
   stall. **Fix:** attach **past the stack-check prologue** — `build_symbols.py`
   computes a per-func `skip` (finds `cmp 0x10(%r14),reg` + the `Jcc`), the shim
   attaches at `base+vaddr+skip`. Args are still in registers there.
2. **park-while-hooked.** A hooked func that *parks* the goroutine on blocking
   I/O stalls agy even past-prologue and on-enter-only. `crypto/tls.(*Conn).Write`
   doesn't park → **safe**; `crypto/tls.(*Conn).Read` and `net/http.RoundTrip`
   park → **stall** (stage 5, reference only).

**The rule (proven both directions):** hook the **crypto** step (CPU-only, runs
after the I/O) — never the **I/O** step (which parks). So:
- **Request:** `crypto/tls.(*Conn).Write` (encrypt side) — captures the model
  request: HTTP/2 + JSON (`"role":"user","parts":[{"text":"<USER_REQUEST>…"`, plus
  system prompt/context/tools in the larger frames).
- **Response:** `crypto/tls.(*halfConn).decrypt` (decrypt side, `on_leave` []byte)
  — captures the decrypted inbound records. It runs *after* the socket read
  parked-and-resumed, on ciphertext already in memory, so it's CPU-only and
  doesn't park (validated: no stall, agy completes, ~150 KB inbound captured).

Both are stage 3. `Read`/`net/http.RoundTrip`/`http2.(*pipe).Write` all **park**
(netpoll / mutex / cond) and stall agy even on-enter-only past-prologue — that's
why we hook decrypt, not read. `AGY_HOOK_PREVIEW` sets capture bytes/event.

The decrypted response is HTTP/2 frames (HPACK headers) with a **compressed body**
(brotli/gzip). `h2reassemble.py` de-frames + de-gzip/brotli/deflate + HPACK-decodes
(`pip install hpack brotli`) — decoding on already-captured bytes, not a hook
concern. (The PTY transcript / `AgyModel` also give the reply if you don't need raw.)

CPU-only funcs (`os.Getenv`, `(*RootModel).Serialize`, `proto.Marshal`) also hook
cleanly; the stage-4 examples show the `[]byte`-return capture (`on_leave`
RAX=ptr, RBX=len).

## Using agy as a TaskSolver backend (`agy/`)

`antigravity/python/agy/` drives agy as a **model backend** with the same adapter
surface as TaskSolver's other providers (mirrors `ClaudeCodeModel`): it shells
out to `agy --print` under a PTY, in a throwaway git workspace, and parses the
reply. No API key (agy is logged in via `~/.gemini/antigravity-cli/`). This is
independent of the gum/LD_PRELOAD instrumentation — it treats agy as a black box.

```python
from tasksolver.common import TaskSpec, Question
from agy import AgyModel                       # on path in the pixi env
model = AgyModel(api_key=None, task=my_task, model="gemini-3-pro")
parsed, raw, meta, payload = model.run_once(Question(["What is 2+2?"]))
```

`import agy` works in the pixi env because `[tool.pixi.activation.env]` puts
`antigravity/python` on `PYTHONPATH`. Smoke test: `pixi run python
antigravity/python/example_agy_backend.py`. For multi-turn scripting use
`agy.InteractiveSession` (PTY + terminal-query responder).

**pixi/WSL1 note:** `pixi install` builds tasksolver as a conda package; on this
WSL1 host the setuptools link step needs the `wsl1-exec.so` shim (already in the
shell's `LD_PRELOAD`), otherwise it fails copying `_distutils_hack/__init__.py`.

## Risks / limitations

- **WSL1**: gum embedded needs `mprotect(PROT_EXEC|PROT_WRITE)` on `.text`;
  validated (see Status). The known WSL1 0-byte-mmap bug is already worked around
  on this host (see MEMORY).
- **agy updates** change offsets and may reorder text again → re-run
  `build_symbols.py`. Names are stable; the build-id guard blocks mismatched hooks.
- **SYNC + GC**: keep SYNC callbacks fast/CPU-bound (above).
- This instruments a binary **you run on your own machine** for research/interop.
  It does not defeat anyone else's security boundary.
