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

then re-apply the emit edits if they drifted, and `pixi run build-codex`.

## Build + run
Built from source as a **gnu-dynamic** ELF (NOT the static-musl release artifact — it must embed
the pixi libpython): `pixi run build-codex` → `vendor/codex-rs/target/release/codex` (point `CODEX_BIN`
elsewhere to override). Needs `rust`, `clang`/`libclang`, `openssl`, `libcap` (pixi deps). Auth:
`OPENAI_API_KEY` or `codex login`. Then:

    from pycodex import ask
    r = ask("What is 2+2?")            # -> CodexResponse(.text, .model, .usage, .request, .turns)
