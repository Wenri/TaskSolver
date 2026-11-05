# Kimi Code linked CLI

`kimi/` builds Moonshot's Node/TypeScript Kimi Code CLI from source with a small
capture patch. The CLI loads an N-API addon that embeds CPython and runs the shared
`wirecap` decoder. `pykimi` exposes one-shot calls, persistent sessions and the
TaskSolver `KimiCodeModel` adapter.

For the native UI, run `pixi run python -m tasksolver.terminal kimi`. It shares the
named-screen manager and optional browser view with agy, Codex and Claude, retaining
Kimi's linked capture. See [managed terminal screens](../README.md#managed-terminal-screens).

## Layout

- `vendor/kimi-code/` — the Kimi Code monorepo, vendored at the release tag pinned in
  [`vendor.json`](vendor.json) (MIT; `LICENSE` preserved). Kept pristine except
  our wiretap patch (below); `node_modules/`, `dist/`, and the build-time `vis-web-asset.ts` stub
  are gitignored. The engine is `packages/agent-core-v2`.
- `native/` — `wirecap_node.cc` + `CMakeLists.txt`: the N-API addon that links
  `libwirecap_bridge.a` (its own copy — `CMakeLists.txt` adds `../../wirecap/native`) and exposes
  `start`/`ready`/`emitRequest`/`emitEvent`/`emitWire`/`shutdown` to JS. Port of codex's Rust
  `wirecap` shim: static-literal kinds, an atomic turn counter, ASYNC-only emits.
- `pykimi/` — the Python wrapper: `ask()`/`KimiResponse`/`KimiCodeModel` + the in-process decode
  side `kimi_process` (the `WIRE_MODULE` the embedded interpreter loads) + the ModelRequestEvent
  turn decoder `kimi_decode`. Kimi 2.4+ appends harness context (date, permission mode) after the
  prompt as extra `<system-reminder>` user messages; the request summary's `first_user_text`
  skips them so it still reports the prompt.

## The patch
A new self-contained emitter `packages/agent-core-v2/src/wiretap/wiretap.ts` (loads the addon
from `$WIRE_NODE_ADDON` when `WIRE_ENABLE` is set; self-initializing on first import; emits are
no-op-safe) plus **three one-line emit sites**:

- `agent/loop/requester.ts` (`AgentLoopRequester`, `agent/llmRequester/llmRequesterService.ts`
  before 2.5.0) — after `recordRequest(logInput)` in `generateModel`'s `onAttempt` →
  `kimi_request` per attempt (the full normalized systemPrompt + tools + messages); inside the
  `for await (… generateModel(…))` loop, before its `switch (event.type)` → `kimi_event` per
  `ModelRequestEvent` (`part`/`usage`/`finish`/`timing`).
- `wire/wireService.ts` `appendRecordLow()` → `kimi_wire` (every appended wire-journal record
  + its agent scope; reading/restoring the journal does not emit records).

The ESM build config also bundles the CLI's runtime dependencies (`ws`, `qrcode`, `chokidar`; tsdown
otherwise leaves `package.json` dependencies external) and embeds the CLI version, so the wheel
runs without the source tree's `node_modules` or `package.json`. After an update, add any new
`apps/kimi-code/package.json` dependency to `alwaysBundle`; `test_kimi_bundle.py` catches a miss.

The wiretap helper is separate from upstream code. Preserve the three emit sites and the
ESM packaging config when updating the source: `python3 tools/runtimes.py adopt kimi
[--version X.Y.Z]` replays the wiretap module, the emit sites and the `tsdown.config.ts` edits onto
a new release with a three-way merge against the pinned tag, keeps the gitignored build output,
and names any conflicted file to fix by hand. `pnpm-lock.yaml` comes from upstream (the build
installs with `--frozen-lockfile`). Then run `pixi reinstall --locked tasksolver`, which rebuilds
the bundle and addon.

## Build + run
Built by **`pixi install`** (setup.py's `_build_kimi`; the addon compiles its own copy of the wirecap bridge):

1. the CLI bundle — `pnpm install --frozen-lockfile` then `tsdown` (invoked as
   `npx -y pnpm@<pin> …`, honoring the vendored `packageManager` field; conda-forge has no pnpm
   10). A stub `vis-web-asset.ts` is written so the vite/tailwind prebuild is skipped (breaks only
   `kimi vis`); invoking tsdown directly (not `pnpm run build`) also skips the darwin/win32
   native-asset copy and the dist-web check. This emits
   `vendor/kimi-code/apps/kimi-code/dist/main.mjs`. A second tsdown invocation with
   `tsdown.dist-worker.config.ts` builds the required sibling `dist/search-worker.mjs`;
   the minidb worker remains SEA-only.
2. the addon — `cmake … kimi/native` → `native/build/wirecap_node.node`, built with GCC LTO
   across the addon and its bridge copy (`TASKSOLVER_LTO`, default on).

All are bundled into the wheel at `pykimi/vendor/{main.mjs,search-worker.mjs,wirecap_node.node}` and resolved
package-only (`wirecap.runtime.vendor.vendored`). The build is **required, with no skip/opt-out**.
The workspace pins Node.js 26 to satisfy the frozen dependency graph (which excludes Node.js 25).
Node supplies node + npm/npx for pnpm and the N-API headers at
`$CONDA_PREFIX/include/node`; the addon links the pixi libpython + Boost like the codex build.
Model: `MOONSHOT_API_KEY`/`KIMI_MODEL_API_KEY` (the env-family definition — no login needed) or
`kimi login`. Then:

    from pykimi import ask
    r = ask("What is 2+2?", model="k3")   # -> KimiResponse(.text, .model, .usage, .request, .turns)

`Agent(..., vision_model="kimi-code", chat=True)` is unsupported; use the HTTP
Kimi adapter for plain chat calls.

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
`KimiResponse.timed_out=True` after the CLI and its process group are reaped.
`exit_status` retains the process's exit status separately from the timeout flag.

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
with pykimi.resume(sid, workspace="/w") as resumed:
    r = resumed.ask("And rename it big_cube.py.")
```

Use a context manager or call `close()` to release the CLI, its descendants and
the result channel. Closing is idempotent; failed startup also unwinds these
resources. A closed or exited session rejects further turns; resume its ID in a
new session.

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

## Tests
Offline, no node/bundle needed (`pixi run python test_scripts/<file>.py`):

- `test_kimi_argv.py` — argv/env assembly, `KIMI_CODE_HOME`/`KIMI_MODEL_NAME` precedence, the
  `mcp_json` PYTHONHOME unwrap, and the session-store readers (index fold, tombstones, cwd
  filter, relocated home).
- `test_kimi_decode.py` — the `kimi_turn` decode (finish-message authoritative, streamed-delta
  fallback, usage normalization, request pairing, skipped `<system-reminder>` messages) + the
  `python3 -S` import-purity probe for
  `pykimi.kimi_process`.
- `test_kimi_process.py` — stub `#!/bin/sh` binaries through the real launch machinery (prompt
  delivery, timeout → `timed_out`, group sweep, no fd leak).
- `test_kimi_session.py` — shell-mode plumbing: persistent argv (no `-p`), the
  production-golden `encode_workdir_key`, the trust record's shape/placement/idempotence,
  legacy and v2 wire→transcript projection (tools, interrupted/resumed steps, compaction,
  and multiple agents), and a stub-backed `Session` (typed turn 1, bracketed-paste framing
  on the raw PTY bytes, close sweep).
- `test_wire_session.py` (repo root `test_scripts/`) — the shared `WireSession`/`ask_turn`
  base, including failed startup, cleanup failures, idempotent close and exited-session guards.

Offline, with Node and the built artifacts:

```bash
pixi run python test_scripts/test_kimi_bundle.py
```

This stages the artifacts through the package's bundling step, then runs outside the
checkout with no `node_modules`: Python artifact resolution, CLI `--version`, and the sibling
search worker opening an empty index. It catches missing npm dependencies or workers that a
source-tree launch can hide. It skips when the build/runtime is absent and fails on a partial
artifact set. These checks do not validate authenticated model calls, and a skip is not a
pass. `kimi vis` remains unavailable in this linked build because the web asset is stubbed.

Live (needs the built bundle + addon):

- `test_kimi.py` — the addon smoke inside a real node process (synthetic emits → `kimi_turn`),
  then a real `kimi -p` turn when a model is configured. Inspect its output: missing artifacts
  or model configuration cause skips, which do not establish provider compatibility.
