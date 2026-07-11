"""Environment + argv wiring for an instrumented codex run.

Unlike pyagy (which LD-preloads a shim into a closed Go binary), codex is built from source with
the wirecap bridge compiled in, and ``codex exec`` is a non-TTY one-shot — so there is no
shim/preload and no PTY. It DOES use the same embedded-worker channel as agy: the launcher
(``codexprocess.CodexPopen``, on the shared ``wirecap.runtime.process`` base) injects
``WIRE_MP_BOOT_FD`` so the bridge's ``wirecap.decode.mp_child`` streams decoded ``codex_turn``s home
over a result queue. This module only builds the run env (the neutral ``WIRE_*`` knobs +
``PYTHONHOME`` + auth passthrough) and the ``codex exec`` argv — ``WIRE_MP_BOOT_FD`` is added by the
launcher, and the bridge also records every turn to the ``WIRE_CAPTURE`` JSONL (which stays
authoritative for the returned turns).
"""
import os

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))   # .../pycodex


def _vendored(in_pkg_rel, sibling_rel):
    """Resolve the codex binary from within the package — never an external path. A self-contained
    wheel bundles it under the package (``pycodex/vendor/codex``); a source/editable checkout keeps
    the cargo output at the sibling ``codex/vendor/codex-rs/target/release/codex``. Prefer
    in-package, fall back to sibling. No env override: codex ships with the package."""
    in_pkg = os.path.join(_PKG_DIR, in_pkg_rel)
    return in_pkg if os.path.exists(in_pkg) else os.path.join(_PKG_DIR, sibling_rel)


# The from-source, wirecap-patched codex (gnu-dynamic; embeds the pixi libpython): the bundled
# pycodex/vendor/codex (wheel) or the cargo output in a checkout.
CODEX_BIN = _vendored("vendor/codex", "../vendor/codex-rs/target/release/codex")


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
