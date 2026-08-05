"""pycodex — a provider wrapper for OpenAI's Codex CLI, sibling to antigravity's pyagy.

Runs the (patched, from-source) codex binary — which has the wirecap bridge compiled in — and
reads the decoded ``codex_turn``s it writes to a capture JSONL. Unlike pyagy/agy this needs no
LD_PRELOAD shim — codex is open source, so the capture hooks are patched into the vendored build
(Phase 6). Everything else matches agy: codex runs under a PTY on the shared
``wirecap.runtime.pty`` bases and streams decoded turns home over the same embedded-worker
mp-child channel. The capture JSONL stays AUTHORITATIVE for the returned turns (``codex exec``
can exit abruptly enough to drop the last streamed one); the live stream is parity + a liveness
probe (``n_streamed``).

Public API:
    pycodex.ask("What is 2+2?")   -> CodexResponse (.text / .model / .usage / .request / .turns)
    pycodex.ask_many(prompt, n)   -> [CodexResponse, ...]
    pycodex.CodexModel(...)       -> a TaskSolver-contract backend (pulls tasksolver — lazy)

Exports are LAZY (PEP 562): importing ``pycodex.codex_process`` (the WIRE_MODULE the embedded
interpreter loads) runs this package __init__, which must stay import-pure — so ``client`` (and
especially ``model``, which imports ``tasksolver``) are imported only on attribute access, never
at package load. The in-process decode side is ``pycodex.codex_process``.
"""

_LAZY = {
    "ask": ".client",
    "ask_many": ".client",
    # Session is the first-class multi-turn object, mirroring pyagy's
    "Session": ".client",
    "resume": ".client",
    "continue_latest": ".client",
    "CodexResponse": ".client",
    "Usage": ".client",
    "CodexModel": ".model",
    # re-exported so callers can seed a workspace before ask() — pycodex.ask's own
    # docstring tells them to, and pyagy exports it under the same name.
    "ensure_git_workspace": "wirecap.runtime.workspace",
    # codex's native session store (read-only), the pyagy.conversations counterpart
    "read_transcript": ".sessions",
    "latest_session_id": ".sessions",
}


def __getattr__(name):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    return getattr(importlib.import_module(mod, __name__), name)


def __dir__():
    return sorted(_LAZY)


__all__ = sorted(_LAZY)   # so `from pycodex import *` binds the lazy names
