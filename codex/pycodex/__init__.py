"""pycodex — a provider wrapper for OpenAI's Codex CLI, sibling to antigravity's pyagy.

Runs the (patched, from-source) codex binary — which has the wirecap bridge compiled in — and
reads the decoded ``codex_turn``s it writes to a capture JSONL. Unlike pyagy/agy this needs no
LD_PRELOAD shim and no PTY: codex is open source (the capture hooks are patched into the vendored
build, Phase 6), and ``codex exec`` is a non-TTY one-shot, so a plain subprocess + a read of the
capture file replaces agy's PTY + embedded-worker streaming.

Public API:
    pycodex.ask("What is 2+2?")           -> CodexResponse (.text / .model / .usage / .request / .turns)
    pycodex.ask_many(prompt, n)           -> [CodexResponse, ...]

The in-process decode side is ``pycodex.codex_process`` (the WIRE_MODULE the embedded interpreter
imports); the OpenAI-Responses turn shaping is ``pycodex.codex_process.responses_decode``.
"""
from .client import CodexResponse, Usage, ask, ask_many   # noqa: F401
