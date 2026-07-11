# codex — instrument OpenAI's Codex CLI (sibling of `antigravity/`)

`antigravity/` wraps the closed-source Go `agy` CLI with an LD_PRELOAD shim; `codex/` does the
same job for OpenAI's **open-source Rust** Codex CLI — but because codex is open source and built
from source, the capture hooks are a small **source patch** at its HTTP boundary rather than
binary hooking. Both share the `wirecap` package (decode + the embedded-CPython native bridge).

## Layout
- `vendor/` — the Codex repo, git-subtree'd at **`rust-v0.143.0-alpha.38`** (Apache-2.0; `LICENSE`
  preserved). Kept pristine except our patch (below); `codex-rs/target/` is gitignored.
- `pycodex/` — the Python wrapper: `ask()`/`CodexResponse`/`CodexModel` + the in-process decode
  side `codex_process` (the `WIRE_MODULE` the embedded interpreter loads) + the OpenAI-Responses
  turn decoder `responses_decode`.

## The patch (Phase 6)
A new self-contained leaf crate `vendor/codex-rs/wirecap/` (FFI onto `libwirecap_bridge.a`) plus
**three one-line emit sites** + `wire_start()` in `cli/src/main.rs`:
- `core/src/client.rs` `build_responses_request()` → `codex_request` (the serialized `/v1/responses`).
- `codex-api/src/sse/responses.rs` and `codex-api/src/endpoint/responses_websocket.rs` → `codex_event`
  at each `ResponsesStreamEvent` deserialize. **Both** transports are patched (WebSocket is the
  default for OpenAI; an SSE-only patch would miss it) — they carry byte-identical JSON, so one
  decoder covers both.

All new code is in the leaf crate (new files never conflict on `subtree pull`); the edits to
existing vendored files are tiny and anchored on stable names, so bumping the pin stays cheap:

    git subtree pull --prefix codex/vendor https://github.com/openai/codex.git <newtag> --squash

then re-apply the emit edits if they drifted, and re-run `pixi install` (which rebuilds codex).

## Build + run
Built from source as a **gnu-dynamic** ELF (NOT the static-musl release artifact — it must embed
the pixi libpython) by **`pixi install`** (setup.py's build_py, after the shim) →
`vendor/codex-rs/target/release/codex`, then bundled into the wheel at `pycodex/vendor/codex` and
resolved package-only. The build is **required, with no skip/opt-out** (a wheel without codex can't
run the `codex` backend). Needs `rust`, `clang`/`libclang`, `openssl`, `libcap` (pixi host-deps).
Auth: `OPENAI_API_KEY` or `codex login`. Then:

    from pycodex import ask
    r = ask("What is 2+2?")            # -> CodexResponse(.text, .model, .usage, .request, .turns)

## Transport
`pycodex` drives codex through the **same wirecap mp-child machinery as agy** (the shared
`wirecap.runtime.process.WirePopen`/`WireProcess` base + `wirecap.decode.mp_child`): `ask()` launches
`codex exec` as a `multiprocessing.spawn` child over a boot pipe, and the compiled-in bridge's
`mp_child` streams decoded `codex_turn`s home over a result `SimpleQueue`. codex is a non-TTY
one-shot (no PTY; unlike agy) with **no terminal signal to Python**, so completion is death-based
(`os.pidfd_open`, or EOF+reap where pidfd is unavailable) and the durable `WIRE_CAPTURE` JSONL stays
**authoritative** for the returned turns — the live stream is a parity bonus surfaced as
`CodexResponse.n_streamed`.
