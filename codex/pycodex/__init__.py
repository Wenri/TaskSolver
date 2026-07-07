"""pycodex — a provider wrapper for OpenAI's Codex CLI, sibling to antigravity's pyagy.

Launches the (patched, from-source) codex binary and captures its ``/v1/responses`` model
traffic into a JSONL, decoded to ``codex_turn``s and streamed home over the shared
embedded-libpython worker (wirecap.native + wirecap.decode). Unlike pyagy/agy this needs no
LD_PRELOAD shim or PTY: codex is open source, so the capture hooks are compiled into the
vendored build, and ``codex exec`` is non-TTY (driven over pipes).

The launcher/client (CodexProcess/ask/Session/CodexModel) land in Phase 7; today this package
holds the Responses turn decoder (``codex_process.responses_decode``).
"""
