# Codex SDK and instrumented CLI

TaskSolver's `CodexModel` and `codex*` model IDs use the official Python SDK over
`codex app-server`. The SDK and Rust CLI ship from the same vendored upstream
snapshot. The CLI retains source patches at its HTTP boundary for native request,
response and usage capture through `wirecap` and its embedded CPython bridge.
Low-level `pycodex.ask` and `pycodex.Session` keep the exec/PTY interfaces.

## Layout
- `vendor/` — the Codex repo, git-subtree'd (originally `rust-v0.143.0-alpha.38`, since bumped to
  **0.157.0** (`rust-v0.157.0`, upstream commit
  `00c972e`) — see `codex-rs/Cargo.toml`; Apache-2.0; `LICENSE` preserved). Kept pristine except
  our patch (below); `codex-rs/target/` is gitignored.
- `pycodex/` — the Python wrapper: `ask()`/`CodexResponse`/`CodexModel` + the in-process decode
  side `codex_process` (the `WIRE_MODULE` the embedded interpreter loads) + the OpenAI-Responses
  turn decoder `responses_decode`. `sdk.py` integrates the official Python SDK with the same
  instrumented binary, while `sdk_process.py` owns its subprocess group.
- `vendor/sdk/python/src/openai_codex/` — the official Python SDK from the same upstream
  snapshot, packaged unchanged with TaskSolver. Its dependencies are `pydantic>=2.12` and
  `packaging>=26.2`; TaskSolver supplies its own compiled CLI instead of installing the SDK's
  separate `openai-codex-cli-bin` runtime.

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

then re-apply the emit edits if they drifted, update SDK dependency bounds if
required, and reinstall TaskSolver in the consuming workspace. The SDK and CLI
must remain on the same upstream snapshot.

## Build + run
Built from source as a **gnu-dynamic** ELF (NOT the static-musl release artifact — it must embed
the pixi libpython) by **`pixi install`** (setup.py's build_py, after the shim) →
`vendor/codex-rs/target/release/codex`, then bundled into the wheel at `pycodex/vendor/codex` and
resolved from the package or its source checkout. The build is **required, with no skip/opt-out** (a wheel without codex can't
run the `codex` backend). Needs `rust`, `clang`/`libclang`, `openssl`, `libcap` (pixi host-deps).
Auth: `OPENAI_API_KEY` or `codex login`. Then:

    from pycodex import ask_sdk
    r = ask_sdk("What is 2+2?")        # SDK answer plus native capture accessors

After changing Python wrappers or vendored sources, refresh installed code and
artifacts with `pixi reinstall --locked tasksolver` in the consuming workspace.
Omit `--locked` when dependency changes require an updated lockfile. Do not
install a separate `openai-codex` or `openai-codex-cli-bin` distribution into
the same environment; TaskSolver supplies both SDK source and runtime.

## Official Python SDK

`ask_sdk()` and `SDKSession` use the [official Codex Python SDK](https://learn.chatgpt.com/docs/codex-sdk)
to drive `codex app-server` over JSON-RPC. `CodexModel` and TaskSolver's existing Codex model
aliases now use this SDK by default. The SDK shares the existing instrumented runtime and
wire capture. The low-level `ask()` and `Session` retain their exec/PTY behavior:

```python
from pycodex import ask_sdk, SDKSession, CodexModel
from openai_codex import LocalImageInput, TextInput

reply = ask_sdk("Describe this project", workspace="/path/to/repo")
print(reply.text, reply.session_id, reply.usage)

with SDKSession(workspace="/path/to/repo", session_id=reply.session_id) as session:
    followup = session.ask([
        TextInput(text="Describe this image in the context of the project."),
        LocalImageInput(path="/absolute/path/image.png"),
    ])
    structured = session.ask(
        "Return the project name",
        output_schema={"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"], "additionalProperties": False},
    )

# Same TaskSolver parsing, retry and metadata contract as the existing adapter:
model = CodexModel(task=my_task, workspace="/path/to/repo")
# Explicit compatibility path:
legacy = CodexModel(task=my_task, transport="exec", workspace="/path/to/repo")
```

The SDK adapter authenticates a supplied or inherited `OPENAI_API_KEY` through the SDK's
`login_api_key` method before starting or resuming a thread, using Codex's process-local
in-memory credential store. It does not replace existing disk or keyring credentials.
Without an API key, it uses the selected `CODEX_HOME`'s existing login.

The SDK accepts typed multimodal inputs and `Thread.run` options such as `output_schema`
and `effort`. `ask_sdk(..., turn_options={...})` forwards those options for a one-shot call.
`CodexModel(..., sdk_options={...})` forwards SDK session settings and one-shot `turn_options`.
`Agent(..., vision_model="codex")` and `vision_model="codex-sdk"` both select the SDK backend.
TaskSolver image payloads are forwarded as native SDK `LocalImageInput` values; task parsing,
parse retries, the four-value return tuple and response metadata attachment are preserved.

`CodexSDKResponse` preserves `CodexResponse`'s text, session ID, capture, request, model and
usage accessors and adds the official `sdk_result` (`TurnResult`). Answers come from the SDK's
final assistant message; request and usage data prefer native capture. If capture is disabled
with `capture=False`, usage falls back to the SDK's reported last usage; request/model remain
unknown without captured model telemetry. SDK reasoning items are available as
`explicit_reasoning_output` and in TaskSolver's response metadata.

Each session has its own capture file. `codex_home=`, `workspace=`, `mcp_servers=`,
`extra_env=`, `codex_bin=`, and `capture=<path>` work with the instrumented runtime;
`config_overrides` accepts TOML `key=value` strings. MCP commands use the same `PYTHONHOME`
unwrap as the exec client. `session_id=` resumes that exact thread, while
`continue_latest=True` selects the newest stored thread for the workspace and fails when
none exists. Thread startup defaults to read-only sandboxing and denied escalations;
`thread_options` accepts the official SDK's `Sandbox` and `ApprovalMode` enum values.

`timeout` bounds initialization and each `SDKSession.ask` turn. A timeout returns
`timed_out=True`, closes the session and reaps its process group, including MCP/tool children.
`CodexModel` converts this response into `TimeoutError` before task parsing.
Failed or interrupted turns raise; they are not successful answers. Closing normally lets
app-server drain native capture before cleaning up. Persistent sessions briefly wait for the
asynchronous native recorder after each SDK result; the full capture file remains available.

For advanced controls, `session.client` and `session.thread` expose the official SDK objects:
`session.thread.turn(prompt).stream()`, steering, interruption and other thread operations.
These direct SDK calls do **not** inherit the wrapper's per-call timeout. Keep them inside
the session context manager so the runtime and its descendants are closed reliably.

## Exec and PTY compatibility transport
> Multi-turn `pycodex.Session` rides the shared `wirecap.runtime.session`
> base (`ask_turn` + `WireSession`) — see `wirecap/runtime/session.py` and
> `test_scripts/test_wire_session.py`; only process construction and the
> `CodexResponse` shape live here.

The low-level `pycodex.ask` and `pycodex.Session` drive codex through the **same wirecap mp-child machinery as agy** (the shared
`wirecap.runtime.process.WirePopen`/`WireProcess` base + `wirecap.decode.mp_child`): `ask()` launches
`codex exec` as a `multiprocessing.spawn` child over a boot pipe, and the compiled-in bridge's
`mp_child` streams decoded `codex_turn`s home over a result `SimpleQueue`. codex runs under the
same PTY flavour as agy (`CodexPopen` on `wirecap.runtime.pty`), but its death sentinel is
`os.pidfd_open` rather than the master — codex spawns tool grandchildren that inherit the pty
slave, so the master can outlive codex itself (EOF+reap where pidfd is unavailable). The durable
`WIRE_CAPTURE` JSONL stays **authoritative** for the returned turns — the live stream is a parity
bonus surfaced as `CodexResponse.n_streamed`.

`ask()` also supports the harness-shaped controls: `codex_home=` scopes codex's whole store
(auth/sessions/rollouts — the env is set for the run AND the post-run store reads use it);
`session_id=`/`continue_latest=` are the non-interactive resume (`codex exec resume` — codex
silently starts a NEW thread when an id does not resolve, so check the store-read
`CodexResponse.session_id`); `prompt_via_stdin=True` delivers the prompt on fd 0 (`codex exec -`,
an unlinked temp file — no quoting/ARG_MAX limits, nothing echoed into the transcript);
`capture=` accepts a path to keep the capture JSONL out of the workspace; `mcp_servers=` renders
`-c mcp_servers.<name>=…` flags via `pycodex.mcp_flags`, wrapping every server command
`/usr/bin/env -u PYTHONHOME` (codex inherits the launcher's `PYTHONHOME` and hands it to the MCP
servers it spawns, which breaks `uv run` venv interpreters — the same policy as
`pyagy.write_mcp_servers`). A run that hits the drain deadline returns with
`CodexResponse.timed_out=True` (codex and its whole process group are reaped by `close()`).

The session readers support both ordinary rollout filenames and the replacement rollouts
created by reverting a thread in Codex 0.157.0:
`rollout-<timestamp>-<thread_id>[_<rollout_id>].jsonl`. Resume always uses the stable
**thread ID**; the replacement rollout ID and a fork's root `session_id` are not aliases for
that thread. History selects its newest rollout, with `session_meta.payload.id` as the
metadata fallback for renamed files. Legacy metadata containing only `session_id` still works.

## Verification

Run the wrapper's offline checks from the TaskSolver root:

```bash
pixi run python test_scripts/test_codex_argv.py
pixi run python test_scripts/test_codex_process.py
pixi run python test_scripts/test_responses_decode.py
pixi run python test_scripts/test_wire_session.py
pixi run python test_scripts/test_codex_sdk.py
pixi run python test_scripts/test_codex_sdk_native.py
```

These cover request arguments, MCP configuration, scoped and reverted session stores,
response decoding, timeout/process cleanup, and the shared persistent-session loop.
The 0.157.0 upgrade also passed the native build, 196 `codex-api` Rust tests, and a local
mock-endpoint smoke that exercised native request/response capture and session readback.
These checks do not establish authenticated service compatibility. To check that separately
after building and authenticating, run `pixi run python test_scripts/test_codex.py`; inspect
its output because missing artifacts or authentication cause it to skip.

The SDK protocol test uses a local stand-in app-server to verify typed input, structured
output options, resumable threads, capture, timeout cleanup and error propagation. The native
SDK smoke runs the bundled Codex against an isolated localhost Responses fixture, verifies
wire capture and continued thread context, and uses a temporary empty `CODEX_HOME`.
