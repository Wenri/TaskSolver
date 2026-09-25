# kimi — instrument Moonshot's Kimi Code CLI (sibling of `codex/` and `antigravity/`)

`antigravity/` wraps the closed-source Go `agy` CLI with an LD_PRELOAD shim; `codex/` patches
OpenAI's open-source Rust codex at its HTTP boundary and compiles the wirecap bridge into the
binary. `kimi/` does the same job for Moonshot's **open-source Node/TypeScript** Kimi Code CLI —
built from source, capture hooks a small **source patch**, and because the host is Node (not a
binary we compile), the embedded-CPython bridge rides an **N-API addon** the patched CLI loads at
startup. All three share the `wirecap` package (decode + the embedded-CPython native bridge).

## Layout
- `vendor/kimi-code/` — the Kimi Code monorepo, git-subtree'd at tag
  **`@moonshot-ai/kimi-code@2.1.1`** (`f67e6398`; MIT; `LICENSE` preserved). Kept pristine except
  our wiretap patch (below); `node_modules/`, `dist/`, and the build-time `vis-web-asset.ts` stub
  are gitignored. The engine is `packages/agent-core-v2`; upstream removed the old v1 engine.
- `native/` — `wirecap_node.cc` + `CMakeLists.txt`: the N-API addon that links
  `libwirecap_bridge.a` (its own copy — `CMakeLists.txt` adds `../../wirecap/native`) and exposes
  `start`/`ready`/`emitRequest`/`emitEvent`/`emitWire`/`shutdown` to JS. Port of codex's Rust
  `wirecap` shim: static-literal kinds, an atomic turn counter, ASYNC-only emits.
- `pykimi/` — the Python wrapper: `ask()`/`KimiResponse`/`KimiCodeModel` + the in-process decode
  side `kimi_process` (the `WIRE_MODULE` the embedded interpreter loads) + the ModelRequestEvent
  turn decoder `kimi_decode`.

## The patch
A new self-contained emitter `packages/agent-core-v2/src/wiretap/wiretap.ts` (loads the addon
from `$WIRE_NODE_ADDON` when `WIRE_ENABLE` is set; self-initializing on first import; emits are
no-op-safe) plus **three one-line emit sites**:
- `agent/llmRequester/llmRequesterService.ts` `run()` — after `recordRequest(logInput)` →
  `kimi_request` (the full normalized systemPrompt + tools + messages); inside the
  `for await (… request.requester.request(…))` loop → `kimi_event` per `ModelRequestEvent`
  (`part`/`usage`/`finish`/`timing`).
- `wire/wireService.ts` `appendRecordLow()` → `kimi_wire` (every appended wire-journal record
  + its agent scope; reading/restoring the journal does not emit records).

The ESM build config also bundles `ws` and `qrcode` and embeds the CLI version so the
wheel runs without the source tree's `node_modules` or `package.json`.

The wiretap helper is separate from upstream code. Preserve the three emit sites and the
ESM packaging config when updating the source:

    git fetch --depth 1 https://github.com/MoonshotAI/kimi-code '+refs/tags/@moonshot-ai/kimi-code@<X.Y.Z>'
    git subtree pull --prefix kimi/vendor/kimi-code FETCH_HEAD --squash

then re-apply the three emit edits if they drifted, and re-run `pixi install` (which rebuilds the
bundle + addon).

## Build + run
Built by **`pixi install`** (setup.py's `_build_kimi`, after the shim so the bridge exists):
1. the CLI bundle — `pnpm install --frozen-lockfile` then `tsdown` (invoked as
   `npx -y pnpm@<pin> …`, honoring the vendored `packageManager` field; conda-forge has no pnpm
   10). A stub `vis-web-asset.ts` is written so the vite/tailwind prebuild is skipped (breaks only
   `kimi vis`); invoking tsdown directly (not `pnpm run build`) also skips the darwin/win32
   native-asset copy and the dist-web check. This emits
   `vendor/kimi-code/apps/kimi-code/dist/main.mjs`. A second tsdown invocation with
   `tsdown.dist-worker.config.ts` builds the required sibling `dist/search-worker.mjs`;
   the minidb worker remains SEA-only.
2. the addon — `cmake … kimi/native` → `native/build/wirecap_node.node`.

All are bundled into the wheel at `pykimi/vendor/{main.mjs,search-worker.mjs,wirecap_node.node}` and resolved
package-only (`wirecap.runtime.vendor.vendored`). The build is **required, with no skip/opt-out**.
The workspace pins Node.js 26 to satisfy the frozen dependency graph (which excludes Node.js 25).
Node supplies node + npm/npx for pnpm and the N-API headers at
`$CONDA_PREFIX/include/node`; the addon links the pixi libpython + Boost like the codex build.
Model: `MOONSHOT_API_KEY`/`KIMI_MODEL_API_KEY` (the env-family definition — no login needed) or
`kimi login`. Then:

    from pykimi import ask
    r = ask("What is 2+2?", model="k3")   # -> KimiResponse(.text, .model, .usage, .request, .turns)

## Transport
`pykimi` drives the CLI through the **same wirecap mp-child machinery as codex/agy** (the shared
`wirecap.runtime.process.WirePopen`/`WireProcess` base + `wirecap.decode.mp_child`): `ask()`
launches `node main.mjs -p <prompt>` as a `multiprocessing.spawn` child over a boot pipe, and the
addon-hosted bridge's `mp_child` streams decoded `kimi_turn`s home over a result `SimpleQueue`.
The CLI runs under the same PTY flavour as codex (`KimiPopen` on `wirecap.runtime.pty`, stdin on
`/dev/null` for print mode), and its death sentinel is `os.pidfd_open` rather than the master —
kimi spawns MCP stdio servers and node-pty shell grandchildren that inherit the pty slave, so the
master can outlive the CLI itself. The durable `WIRE_CAPTURE` JSONL stays **authoritative** for
the returned turns — the live stream is a parity bonus surfaced as `KimiResponse.n_streamed`.

The bridge embeds CPython inside node. Because node `dlopen`s the addon `RTLD_LOCAL`, the addon
promotes the embedded libpython to the global namespace (`dlopen(soname, RTLD_GLOBAL|RTLD_NOLOAD)`)
before `wire_start()` — otherwise the interpreter's stdlib C extensions (`_hashlib`, `_json`, …)
can't resolve the Python C-API. Module resolution is then the standard bridge contract:
`PYTHONHOME=$CONDA_PREFIX` → the env's site-packages carries `pykimi` + `wirecap` (no
`PYTHONPATH`); the addon and libpython must be the same Python minor the wheel was built against.

`ask()` also supports the harness-shaped controls: `kimi_home=` scopes the CLI's whole store
(`$KIMI_CODE_HOME` — config/sessions/wire journals; set for the run AND the post-run store reads);
`session_id=`/`continue_latest=` resume a stored session for the working directory (`-S <id>` /
`--continue` — check the store-read `KimiResponse.session_id`, not an echo); `model=` rides the
env-family `KIMI_MODEL_NAME`; `mcp_servers=` writes the workspace-local `.kimi-code/mcp.json` the
CLI auto-discovers (kimi-code has no MCP flag), wrapping every server command
`/usr/bin/env -u PYTHONHOME` (the CLI inherits the launcher's `PYTHONHOME` for the embedded
interpreter and hands it to the MCP servers it spawns, which breaks `uv run` venv interpreters —
same policy as `pycodex.mcp_flags` / `pyagy.write_mcp_servers`); `capture=` accepts a path to keep
the capture JSONL out of the workspace. A run that hits the drain deadline returns with
`KimiResponse.timed_out=True` (and a negative `exit_status`) after the group teardown.

Print mode needs no approval flag: it forces `auto` permission and auto-approves every tool
call for the turn. It also **rejects** `--yolo`/`-y`/`--auto`/`--plan` (`Cannot combine
--prompt with …`), and the CLI then exits 1 with nothing on stdout — which would surface
here as an empty-output failure far from the cause, so `kimi_argv` raises on them instead.
Those flags are meaningful only in shell mode.

## Sessions

`pykimi.Session` is the multi-turn counterpart of `pyagy.Session`/`pycodex.Session`, on the
shared `wirecap.runtime.session.WireSession` base: in-run turns ride ONE live kimi-code
**shell UI** (`kimi` with no `-p` — shell mode is selected by the absence of the flag), and
across restarts the native store resumes via `session_id=` (`-S <id>`) /
`continue_latest=True` (`--continue`), or the module helpers `pykimi.resume` /
`pykimi.continue_latest`.

```python
import pykimi
with pykimi.Session(workspace="/w", model="k3") as s:
    a = s.ask("Create cube.py that makes a 2m cube.")
    b = s.ask("Now double its size.")          # same CLI process, full context
    sid = s.session_id                          # store-read after turn 1
r = pykimi.resume(sid, workspace="/w").ask("And rename it big_cube.py.")
```

Shell-mode specifics, each with its own machinery here:
- **No positional prompt**: turn 1 is *typed* like every follow-up
  (`WireSession._PREFILL_FIRST = False`); `kimi_argv(persistent=True)` rejects one.
- **Bracketed paste**: kimi's editor submits on every typed newline, so
  `KimiProcess.submit` wraps multi-line prompts in `ESC[200~ … ESC[201~` with the CR sent
  separately — one insertion, one turn.
- **Trust gate**: the TUI opens with a folder-trust dialog for an unknown workspace.
  `Session._pre_start` seeds `<home>/workspace-trust/<encodeWorkDirKey(cwd)>` with the CLI's
  exact record shape (`pykimi.config.trust_workspace`, key port verified against a
  production-minted key), so the dialog never renders; `KimiPopen._answer` accepts it as the
  fallback. Print-mode-rejected flags (`--yolo`, …) are legal session `extra_flags`.
- **History**: `Session.history()` / `pykimi.sessions.read_transcript` project the store's
  wire journals into the same
  `{step_index, role, type, created_at, content}` shape `pycodex.sessions.read_transcript`
  returns. Older `context.append_message` records and Kimi 2.x
  `context.append_loop_event` records are supported together. Assistant steps and tool
  results are folded per agent, including interrupted/resumed steps and messages injected
  while tools are pending; journals from separate agents do not share fold state.

Transport note: `kimi acp` (a JSON-RPC agent protocol) would be architecturally cleaner — no
PTY, no idle heuristics — but it is a fourth transport shape that bypasses
`ask_turn`/`WirePtyProcess` entirely, defeating the shared-base symmetry with agy/codex. It is
the documented fallback if PTY shell mode proves unreliable, not the default.

## Tests
Offline, no node/bundle needed (`python3 test_scripts/<f>.py`):
- `test_kimi_argv.py` — argv/env assembly, `KIMI_CODE_HOME`/`KIMI_MODEL_NAME` precedence, the
  `mcp_json` PYTHONHOME unwrap, and the session-store readers (index fold, tombstones, cwd
  filter, relocated home).
- `test_kimi_decode.py` — the `kimi_turn` decode (finish-message authoritative, streamed-delta
  fallback, usage normalization, request pairing) + the `python3 -S` import-purity probe for
  `pykimi.kimi_process`.
- `test_kimi_process.py` — stub `#!/bin/sh` binaries through the real launch machinery (prompt
  delivery, timeout → `timed_out`, group sweep, no fd leak).
- `test_kimi_session.py` — shell-mode plumbing: persistent argv (no `-p`), the
  production-golden `encode_workdir_key`, the trust record's shape/placement/idempotence,
  legacy and v2 wire→transcript projection (tools, interrupted/resumed steps, compaction,
  and multiple agents), and a stub-backed `Session` (typed turn 1, bracketed-paste framing
  on the raw PTY bytes, close sweep).
- `test_wire_session.py` (repo root `test_scripts/`) — the shared `WireSession`/`ask_turn`
  base all three providers' Sessions ride.

Offline, with Node and the built artifacts:

```bash
pixi run python test_scripts/test_kimi_bundle.py
```

This stages the artifacts through the package's bundling hook, then runs outside the
checkout with no `node_modules`: Python artifact resolution, CLI `--version`, and the sibling
search worker opening an empty index. It catches missing npm dependencies or workers that a
source-tree launch can hide. It skips when the build/runtime is absent and fails on a partial
artifact set. The 2.1.1 upgrade passed this check and the wrapper's offline suites;
authenticated model turns were not revalidated. `kimi vis` remains unavailable in this
instrumented build because the web asset is stubbed.

Live (needs the built bundle + addon; skips cleanly = exit 0 otherwise):
- `test_kimi.py` — the addon smoke inside a real node process (synthetic emits → `kimi_turn`),
  then a real `kimi -p` turn when a model is configured.
