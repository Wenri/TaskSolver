"""Environment + argv wiring for an instrumented codex run.

Unlike pyagy (which LD-preloads a shim into a closed Go binary under a PTY), codex is built
from source with the wirecap bridge compiled in, and ``codex exec`` is a non-TTY one-shot — so
this is much simpler: no shim/preload, no PTY, no embedded-worker channel. We just point the
already-instrumented codex at a capture JSONL via the neutral ``WIRE_*`` knobs and run it; the
bridge decodes ``codex_turn``s into that file, which the client reads after the run.
"""
import os

# codex/ — holds the pycodex package + the vendored + built codex binary.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../codex
# The from-source, wirecap-patched codex (gnu-dynamic; embeds the pixi libpython). Override with
# CODEX_BIN. Built by `pixi run build-codex`.
CODEX_BIN = os.environ.get("CODEX_BIN") or os.path.join(
    ROOT, "vendor", "codex-rs", "target", "release", "codex")


def instrumented_env(capture, module="pycodex.codex_process", base=None, extra_env=None):
    """Environment that enables the wirecap bridge in codex and points it at ``capture``.

    Sets the neutral bridge contract (``WIRE_ENABLE`` gates the bridge on; ``WIRE_MODULE`` is the
    dispatch module). No sys.path is injected: the bridge runs `site`, so the embedded interpreter
    imports pycodex/wirecap from its own env's site-packages — the same install the parent runs.
    The binary bakes no RPATH and embeds the build env's libpython, so the caller's env must be
    same-version with the build env: its ``LD_LIBRARY_PATH`` supplies libpython/Boost, and
    ``PYTHONHOME`` points the embedded interpreter at that env's stdlib + site-packages.
    ``OPENAI_API_KEY`` (if set) is inherited for API-key auth; otherwise codex uses its
    ``~/.codex/auth.json`` login."""
    env = dict(base if base is not None else os.environ)
    env["WIRE_ENABLE"] = "1"
    env["WIRE_MODULE"] = module
    env["WIRE_CAPTURE"] = os.path.abspath(capture)
    conda = env.get("CONDA_PREFIX")
    if conda and not env.get("PYTHONHOME"):
        env["PYTHONHOME"] = conda          # embedded interpreter finds the conda stdlib + site-packages
    env.setdefault("CODEX_DISABLE_UPDATE_CHECK", "1")
    if extra_env:
        env.update(extra_env)
    return env


def codex_argv(prompt, workspace, model=None, extra_flags=None, codex_bin=None):
    """codex's non-interactive one-shot argv: ``codex exec <prompt> --skip-git-repo-check -C <ws>``
    (+ ``-m <model>`` / extra flags). Run with stdin closed so ``exec`` doesn't block reading it."""
    argv = [codex_bin or CODEX_BIN, "exec", prompt, "--skip-git-repo-check", "-C", workspace]
    if model:
        argv += ["-m", model]
    if extra_flags:
        argv += list(extra_flags)
    return argv
