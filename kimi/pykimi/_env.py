"""Environment + argv wiring for an instrumented kimi-code run.

kimi-code is a Node CLI, so unlike codex (bridge compiled into the binary) the wirecap bridge
rides an N-API addon: the vendored source's wiretap patch loads ``$WIRE_NODE_ADDON`` when
``WIRE_ENABLE`` is set, and the addon (``kimi/native/wirecap_node.cc``) links the same
``libwirecap_bridge.a`` codex embeds. Everything else matches codex: the launcher
(``kimiprocess.KimiPopen``, on the shared ``wirecap.runtime.pty`` bases) runs the CLI under a PTY
and injects ``WIRE_MP_BOOT_FD`` so the bridge's ``wirecap.decode.mp_child`` streams decoded
``kimi_turn``s home over a result queue. This module only builds the run env (the neutral
``WIRE_*`` knobs + ``PYTHONHOME`` + the ``KIMI_MODEL_*`` model definition) and the argv —
``WIRE_MP_BOOT_FD`` is added by the launcher.
"""
import os
import shutil

from wirecap.runtime.vendor import vendored

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))   # .../pykimi


def _vendored(in_pkg_rel, sibling_rel):
    """kimi-side binding of the shared resolver (wirecap.runtime.vendor)."""
    return vendored(_PKG_DIR, "pykimi", in_pkg_rel, sibling_rel)


# The tsdown-built single-file CLI bundle from the vendored (wiretap-patched) source: the bundled
# pykimi/vendor/main.mjs (wheel) or the build output in a checkout.
KIMI_MAIN = _vendored("vendor/main.mjs", "../vendor/kimi-code/apps/kimi-code/dist/main.mjs")

# The N-API addon hosting the wirecap bridge (embedded CPython). Resolved here and handed to the
# CLI via $WIRE_NODE_ADDON — the vendored wiretap patch loads exactly this path.
WIRE_NODE_ADDON = _vendored("vendor/wirecap_node.node", "../native/build/wirecap_node.node")


def node_bin():
    """The Node runtime for the bundle — the env's node (the addon links the env's libpython,
    same-env coupling as codex's gnu-dynamic build), else PATH."""
    conda = os.environ.get("CONDA_PREFIX")
    if conda:
        candidate = os.path.join(conda, "bin", "node")
        if os.path.exists(candidate):
            return candidate
    return shutil.which("node") or "node"


def instrumented_env(capture, module="pykimi.kimi_process", base=None, extra_env=None,
                     kimi_home=None, model=None):
    """Environment that enables the wiretap addon in kimi-code and points it at ``capture``.

    Sets the neutral bridge contract (``WIRE_ENABLE`` gates the addon load; ``WIRE_MODULE`` is the
    dispatch module; ``WIRE_NODE_ADDON`` is the addon path the vendored patch requires). No
    sys.path is injected: the bridge runs `site`, so the embedded interpreter imports
    pykimi/wirecap from its own env's site-packages — ``PYTHONHOME`` points it at that env.
    ``WIRE_MAXCOPY`` is raised to 8 MiB because a ``kimi_request`` carries the full message
    history (the bridge silently truncates over the cap, which would null the decoded request).

    The model rides the CLI's env-var family: ``model=`` sets ``KIMI_MODEL_NAME`` (winning over
    ``extra_env``); the rest of the definition (``KIMI_MODEL_API_KEY`` / ``_BASE_URL`` /
    ``_PROVIDER_TYPE`` / ...) comes via ``extra_env`` — or the CLI's own login/config when absent.
    """
    env = dict(base if base is not None else os.environ)
    env["WIRE_ENABLE"] = "1"
    env["WIRE_MODULE"] = module
    env["WIRE_CAPTURE"] = os.path.abspath(capture)
    env["WIRE_NODE_ADDON"] = os.path.abspath(WIRE_NODE_ADDON)
    env.setdefault("WIRE_MAXCOPY", "8388608")
    conda = env.get("CONDA_PREFIX")
    if conda and not env.get("PYTHONHOME"):
        env["PYTHONHOME"] = conda          # embedded interpreter finds the conda stdlib + site-packages
    env.setdefault("KIMI_CODE_NO_AUTO_UPDATE", "1")
    if extra_env:
        env.update(extra_env)
    if model:
        env["KIMI_MODEL_NAME"] = model
    if kimi_home:
        # First-class store scoping (config/sessions/wire journals). The explicit kwarg
        # wins over anything extra_env carried, so callers cannot half-scope.
        env["KIMI_CODE_HOME"] = os.path.abspath(kimi_home)
    return env


#: Flags kimi-code rejects in print mode. `cli/options.ts` raises
#: `Cannot combine --prompt with <flag>` for each of these and the CLI exits 1
#: with nothing on stdout, which surfaces here as an empty-output failure a long
#: way from the cause. Print mode needs none of them: it already forces
#: `setPermission('auto')` and installs an auto-approving handler for the turn
#: (`cli/run-prompt.ts`). They are legal — and meaningful — in shell mode, so
#: the guard only fires on the print-mode branch.
PRINT_MODE_REJECTS = ("--yolo", "-y", "--auto", "--plan")


def kimi_argv(prompt, extra_flags=None, kimi_bin=None, session_id=None,
              continue_latest=False, persistent=False):
    """kimi-code's argv tail. One-shot print mode is ``kimi -p <prompt>`` — the counterpart of
    ``codex exec`` / ``agy --print``. ``persistent=True`` is shell mode, selected by the *absence*
    of ``-p``: the CLI opens its interactive TUI, takes no positional prompt (the caller types
    turns via ``KimiProcess.submit``), and accepts the flags print mode rejects. ``session_id`` /
    ``continue_latest`` resume a stored session for the working directory (``-S <id>`` /
    ``--continue``) in either mode, mirroring codex's ``exec resume <id>`` / ``resume --last``.
    There is no ``--work-dir``-style flag worth using: the run's cwd comes from the launcher's
    chdir(workdir), which is also what scopes the store's session index. ``kimi_bin`` substitutes
    a single executable for ``node main.mjs`` (tests use ``#!/bin/sh`` stubs; there is
    deliberately no env override for the real bundle).

    Raises ``ValueError`` for a flag print mode rejects (:data:`PRINT_MODE_REJECTS`) rather than
    letting the CLI exit 1 with no output, and for a positional prompt in shell mode (which has
    nowhere to put one)."""
    argv = [kimi_bin] if kimi_bin else [node_bin(), KIMI_MAIN]
    if session_id:
        argv += ["-S", session_id]
    elif continue_latest:
        argv += ["--continue"]
    if persistent:
        if prompt:
            raise ValueError(
                "shell mode takes no positional prompt — the first turn is typed "
                "into the TUI (KimiProcess.submit), not passed on the argv")
        if extra_flags:
            argv += list(extra_flags)   # --yolo & friends are legal (and meaningful) here
        return argv
    argv += ["-p", prompt or ""]
    if extra_flags:
        for flag in extra_flags:
            if flag in PRINT_MODE_REJECTS:
                raise ValueError(
                    f"kimi-code rejects {flag} in print mode "
                    f"(`Cannot combine --prompt with {flag}`) and exits 1 with no output. "
                    f"Print mode already auto-approves every tool call, so the flag is "
                    f"redundant here; drop it (shell-mode sessions may pass it).")
        argv += list(extra_flags)
    return argv
