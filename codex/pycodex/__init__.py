"""pycodex — a provider wrapper for OpenAI's Codex CLI, sibling to antigravity's pyagy.

Runs the (patched, from-source) codex binary — which has the wirecap bridge compiled in — and
reads the decoded ``codex_turn``s it writes to a capture JSONL. Unlike pyagy/agy this needs no
LD_PRELOAD shim and no PTY: codex is open source (the capture hooks are patched into the vendored
build, Phase 6), and ``codex exec`` is a non-TTY one-shot, so a plain subprocess + a read of the
capture file replaces agy's PTY + embedded-worker streaming.

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
    "CodexResponse": ".client",
    "Usage": ".client",
    "CodexModel": ".model",
}


def __getattr__(name):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    return getattr(importlib.import_module(mod, __name__), name)


def __dir__():
    return sorted(_LAZY)
