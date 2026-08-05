"""wirecap — provider-neutral capture/decode of agent↔model wire traffic.

Shared foundation for the CLI-wrapper subsystems (`pyagy` for the Go `agy` CLI under
`antigravity/`, `pycodex` for OpenAI's Rust Codex CLI under `codex/`). Three layers:

- ``wirecap.decode``  — stdlib-pure: the JSONL ``Recorder``, HTTP/1.1+SSE framing, the
  request/response ``BaseCorrelator``, HTTP/2 reassembly, the embedded-worker mp-child
  runner, and the ``TurnBuilder`` base (+ the shared ``Usage``/turn helpers). (Egress
  ``rewrite`` is NOT here — it is agy-specific and lives in ``pyagy.agy_process.rewrite``.) This layer is imported by the
  embedded interpreter inside the instrumented CLI, so it MUST stay import-pure (stdlib +
  lazily-imported optionals only) — never import ``wirecap.runtime`` or ``tasksolver`` here.
- ``wirecap.runtime`` — parent-side driver (PTY/pipe launch, terminal glue, the spawn-process
  handle, client drain loops, git-workspace scoping). Non-stdlib deps are fine here.
- ``wirecap.native``  — the CPython-embedding worker (``pybridge.cpp`` + ``wirecap.h``) built
  as ``wirecap_bridge`` and linked by both the antigravity shim and the codex build; it calls
  ``dispatch(kind, stream_id, data)`` on the module named by ``WIRE_MODULE``.

Kept intentionally empty (no eager imports) so ``import wirecap.decode`` under the embedded
interpreter never drags in the runtime layer.
"""
