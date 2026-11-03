"""Environment + argv wiring for a linked codex run.

Codex is built from source with the wirecap bridge compiled in; agy links its converted Go
binary into a C++ host. Their launchers share the same runtime: the
launcher (``codexprocess.CodexPopen``, on the shared ``wirecap.runtime.pty`` bases) runs codex under
a PTY and injects ``WIRE_MP_BOOT_FD`` so the bridge's ``wirecap.decode.mp_child`` streams decoded
``codex_turn``s home over a result queue. This module only builds the run env (the neutral ``WIRE_*``
knobs + ``PYTHONHOME`` + auth passthrough) and the argv — ``WIRE_MP_BOOT_FD`` is added by the
launcher, and the bridge also records every turn to the ``WIRE_CAPTURE`` JSONL (which stays
authoritative for the returned turns).
"""
import os
from collections.abc import Mapping, Sequence

from wirecap.runtime.vendor import python_home, vendored

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))   # .../pycodex


def _vendored(in_pkg_rel: str, sibling_rel: str) -> str:
    """codex-side binding of the shared resolver (wirecap.runtime.vendor). Now includes the
    sys.path fallback pyagy always had — a development checkout without the cargo output can
    reuse an installed wheel's binary."""
    return vendored(_PKG_DIR, "pycodex", in_pkg_rel, sibling_rel)


# The from-source, wirecap-patched codex (gnu-dynamic; embeds the pixi libpython): the bundled
# pycodex/vendor/codex (wheel) or the cargo output in a checkout.
CODEX_BIN = _vendored("vendor/codex", "../vendor/codex-rs/target/release/codex")


def linked_env(capture: str | os.PathLike[str], module: str = "pycodex.codex_process",
               base: Mapping[str, str] | None = None,
               extra_env: Mapping[str, str] | None = None,
               codex_home: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Environment that enables the wirecap bridge in codex and points it at ``capture``.

    Sets the neutral bridge contract (``WIRE_ENABLE`` gates the bridge on; ``WIRE_MODULE`` is the
    dispatch module). No sys.path is injected: the bridge runs `site`, so the embedded interpreter
    imports pycodex/wirecap from its own env's site-packages — the same install the parent runs.
    The binary's RUNPATH resolves libpython/Boost relative to the packaged executable. The
    caller must use the same Python minor version; ``PYTHONHOME`` selects that environment's
    stdlib and site-packages for the embedded interpreter.
    ``OPENAI_API_KEY`` (if set) is inherited for API-key auth; otherwise codex uses its
    ``~/.codex/auth.json`` login."""
    env = dict(base if base is not None else os.environ)
    env["WIRE_ENABLE"] = "1"
    env["WIRE_MODULE"] = module
    env["WIRE_CAPTURE"] = os.path.abspath(capture)
    home = python_home(env)
    if home and not env.get("PYTHONHOME"):
        env["PYTHONHOME"] = home           # embedded interpreter finds this env's stdlib + site-packages
    env.setdefault("CODEX_DISABLE_UPDATE_CHECK", "1")
    if extra_env:
        env.update(extra_env)
    if codex_home:
        # First-class store scoping (auth/sessions/rollouts). The explicit kwarg
        # wins over anything extra_env carried, so callers cannot half-scope.
        env["CODEX_HOME"] = os.path.abspath(codex_home)
    return env


def codex_argv(prompt: str | None, workspace: str, model: str | None = None,
               extra_flags: Sequence[str] | None = None, codex_bin: str | None = None,
               persistent: bool = False, session_id: str | None = None,
               continue_latest: bool = False, prompt_via_stdin: bool = False) -> list[str]:
    """codex's argv tail. One-shot is ``codex exec <prompt> --skip-git-repo-check -C <ws>`` (run with
    stdin on /dev/null so ``exec`` doesn't block reading it) — the direct counterpart of agy's
    ``--print``. ``persistent=True`` launches the interactive TUI (bare ``codex <prompt>``), the
    counterpart of agy's ``--prompt-interactive``; ``session_id`` / ``continue_latest`` resume a
    stored session (``codex resume <id>`` / ``codex resume --last``), mirroring agy's
    ``--conversation=<id>`` / ``--continue``. ``prompt_via_stdin`` replaces the
    positional prompt with ``-`` (codex reads stdin to EOF) — one-shot only; the
    launcher supplies fd 0."""
    bin_ = codex_bin or CODEX_BIN
    if prompt_via_stdin and not persistent:
        prompt = "-"
    if persistent:
        # Interactive TUI: bare `codex`, options forwarded to the TUI CLI. NOTE
        # --skip-git-repo-check is `global = true` only INSIDE the exec subtree
        # (exec/src/cli.rs), so it must not be passed here. Top-level `codex resume`
        # resumes an INTERACTIVE session (cli/src/main.rs) — the TUI counterpart of
        # agy's --conversation=<id> / --continue.
        if session_id:
            argv = [bin_, "resume", session_id]
        elif continue_latest:
            argv = [bin_, "resume", "--last"]
        else:
            argv = [bin_, *([prompt] if prompt else [])]
        # The bundled binary has no upstream daemon package layout. Keep its
        # app-server and capture worker owned by this interactive process.
        argv += ["-C", workspace, "--no-daemon"]
    else:
        # One-shot, the direct counterpart of `agy --print`. `codex exec resume` is the
        # non-interactive resume (exec/src/cli.rs Command::Resume); with --last the
        # positional is reinterpreted as the prompt, not a session id.
        argv = [bin_, "exec"]
        if session_id:
            argv += ["resume", session_id, *([prompt] if prompt else [])]
        elif continue_latest:
            argv += ["resume", "--last", *([prompt] if prompt else [])]
        else:
            argv += [prompt or ""]
        # -C/--cd is NOT marked global in exec's clap tree (mark_exec_global_args
        # covers only model/danger-bypass/bypass_hook_trust), so after the
        # `resume` subcommand token it is rejected — the resumed run's cwd is the
        # launcher's chdir(workdir) instead, matching `codex exec resume` usage.
        argv += ["--skip-git-repo-check"]
        if not (session_id or continue_latest):
            argv += ["-C", workspace]
    if model:
        argv += ["-m", model]
    if extra_flags:
        argv += list(extra_flags)
    return argv
