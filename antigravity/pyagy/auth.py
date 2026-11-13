"""Dedicated file-backed authentication and account switching for ``pyagy``.

``agy`` normally stores a single global OAuth token in the OS D-Bus keyring under the
fixed key ``service="antigravity", user="gemini"``, shared across every ``--app_data_dir``
and ``HOME``. To run ``TaskSolver`` / ``pyagy`` under a separate Google account without
logging out or overwriting the user's interactive ``agy`` session
(``~/.gemini/antigravity-cli``), ``pyagy`` isolates credentials per store:

  1. Unlinks any symlinks from ``~/.gemini/<app_data_dir>/`` into
     ``~/.gemini/antigravity-cli/`` and writes ``.dedicated_auth``.
  2. Sets ``WSL_INTEROP=pyagy-isolated``, which ``agy`` reads only inside
     ``codeassistclient.wslDetector.Detect`` to bypass the OS keyring and store
     ``antigravity-oauth-token`` strictly inside ``~/.gemini/<app_data_dir>/``.
  3. Uses ``AGY_CLI_CDE_AUTH_ACTION=login`` / ``check`` via a temporary ``HOME`` bridge
     so login and verification run standalone and exit immediately without opening the TUI
     or creating conversations.

CLI usage::

    python -m pyagy.auth status
    python -m pyagy.auth switch --oob          # sign into another account (URL + code paste)
    python -m pyagy.auth switch                # sign into another account (local browser redirect)
    python -m pyagy.auth switch --account alt  # dedicated profile ~/.gemini/pyagy-alt
    python -m pyagy.auth logout
    python -m pyagy.auth shared                # return to sharing global ~/.gemini/antigravity-cli
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import glob
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Generator
from typing import Final, Literal

from . import conversations as _conv
from ._env import _vendored

type AuthMode = Literal["dedicated", "shared", "global"]

_VENDOR_AGY: Final = _vendored("vendor/agy", "../vendor/agy")
_AUTH_EMAIL_RE: Final = re.compile(r"(?:Already|Successfully) authenticated as (\S+?)\.(?:\s|$)")
_CREDENTIAL_FILES: Final = (
    "antigravity-oauth-token",
    "installation_id",
    "acp_auth_state.json",
    "jetski_state.pbtxt",
)
_CREDENTIAL_CACHE_FILES: Final = (
    "default_project_id.txt",
    "projects.json",
    "onboarding.json",
    "antigravity-keyring-unavailable",
)

__all__ = [
    "AuthMode",
    "AuthStatus",
    "auth_status",
    "enable_dedicated_auth",
    "list_auth_stores",
    "login",
    "logout",
    "use_shared_auth",
]


@dataclass(frozen=True)
class AuthStatus:
    app_data_dir: str
    store: str
    mode: AuthMode
    has_token_file: bool
    project_id: str | None = None
    authenticated: bool | None = None
    email: str | None = None


def _remove_local_entry(path: str) -> None:
    """Remove a file or symlink at ``path`` without following symlinks."""
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


def _read_project_id(root: str) -> str | None:
    path = os.path.join(root, "cache", "default_project_id.txt")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            val = f.read().strip()
        return val or None
    except OSError:
        return None


def _require_non_global_store(app_data_dir: str | None, account: str | None) -> tuple[str, str, str]:
    adir = _conv._validate_app_data_dir(_conv.resolve_app_data_dir(app_data_dir, account=account))
    real_gem = os.path.expanduser("~/.gemini")
    real_app = os.path.abspath(os.path.join(real_gem, _conv.GLOBAL_APP_DATA_DIR))
    root = os.path.abspath(os.path.join(real_gem, adir))
    if adir == _conv.GLOBAL_APP_DATA_DIR or root == real_app:
        raise ValueError(
            f"Refusing to modify global interactive agy store ({real_app}); "
            "specify a dedicated store (default 'pyagy') or --account <name>.")
    return adir, real_app, root


def enable_dedicated_auth(app_data_dir: str | None = None, *,
                          account: str | None = None,
                          clear_credentials: bool = False) -> str:
    """Pin ``~/.gemini/<app_data_dir>`` to isolated file-backed auth and return its path.

    Unlinks any symlinks pointing into ``~/.gemini/antigravity-cli`` so ``agy`` cannot read
    or overwrite global files, writes ``.dedicated_auth``, and when ``clear_credentials`` is
    set removes existing local OAuth/project cache files inside the target store so a fresh
    account sign-in starts clean.
    """
    adir, _real_app, root = _require_non_global_store(app_data_dir, account)
    _conv.prepare_app_data_dir(adir, isolated_auth=True, account=account)
    if clear_credentials:
        for name in _CREDENTIAL_FILES:
            _remove_local_entry(os.path.join(root, name))
        cache_dir = os.path.join(root, "cache")
        for cache_name in _CREDENTIAL_CACHE_FILES:
            _remove_local_entry(os.path.join(cache_dir, cache_name))
    return root


def logout(app_data_dir: str | None = None, *, account: str | None = None) -> str:
    """Clear dedicated credentials in ``~/.gemini/<app_data_dir>`` while keeping it isolated
    from the global OS keyring (``.dedicated_auth`` stays set)."""
    return enable_dedicated_auth(app_data_dir, account=account, clear_credentials=True)


def use_shared_auth(app_data_dir: str | None = None, *, account: str | None = None) -> str:
    """Switch ``~/.gemini/<app_data_dir>`` back to sharing the global ``~/.gemini/antigravity-cli`` login."""
    adir, _real_app, root = _require_non_global_store(app_data_dir, account)
    _remove_local_entry(os.path.join(root, _conv.DEDICATED_AUTH_MARKER))
    for name in _CREDENTIAL_FILES:
        _remove_local_entry(os.path.join(root, name))
    cache_dir = os.path.join(root, "cache")
    for cache_name in _CREDENTIAL_CACHE_FILES:
        _remove_local_entry(os.path.join(cache_dir, cache_name))
    return _conv.prepare_app_data_dir(adir, isolated_auth=False)


@contextmanager
def _auth_action_bridge(root: str, *, isolated: bool) -> Generator[str]:
    """Create a temporary ``HOME`` routing ``AGY_CLI_CDE_AUTH_ACTION`` to ``root``.

    ``AGY_CLI_CDE_AUTH_ACTION`` runs in ``agy``'s ``main.main`` before CLI flag parsing
    when ``len(os.Args) == 1`` and calls ``auth.defaultTokenStorage()`` ->
    ``codeassistclient.NewFileTokenStorage()``, which reads and writes
    ``$HOME/.gemini/jetski-standalone-oauth-token`` (the exact same ``StoredToken`` JSON
    format that ``NewCLITokenStorage`` reads from ``<app_data_dir>/antigravity-oauth-token``).
    When ``isolated`` is true (``WSL_INTEROP=pyagy-isolated``), bridging
    ``jetski-standalone-oauth-token`` to ``<root>/antigravity-oauth-token`` routes standalone
    login/check directly into ``root``; when ``isolated`` is false, keyring mode is active and
    we must not bridge ``jetski-standalone-oauth-token`` (otherwise ``compositeTokenStorage``
    cleans up the fallback token file after reading the keyring).
    """
    import shutil

    os.makedirs(root, exist_ok=True)
    real_cfg = os.path.expanduser("~/.gemini/config")
    target_token = os.path.join(root, "antigravity-oauth-token")
    with tempfile.TemporaryDirectory(prefix="pyagy-auth-") as bridge_home:
        gem = os.path.join(bridge_home, ".gemini")
        os.makedirs(gem, mode=0o700, exist_ok=True)
        standalone_token = os.path.join(gem, "jetski-standalone-oauth-token")
        if isolated:
            os.symlink(target_token, standalone_token)
        os.symlink(root, os.path.join(gem, _conv.GLOBAL_APP_DATA_DIR))
        if os.path.exists(real_cfg):
            os.symlink(real_cfg, os.path.join(gem, "config"))
        try:
            yield bridge_home
        finally:
            if isolated and os.path.isfile(standalone_token) and not os.path.islink(standalone_token):
                shutil.copy2(standalone_token, target_token)
                os.chmod(target_token, 0o600)


def _auth_action_env(bridge_home: str, action: Literal["check", "login"], *,
                     isolated: bool, oob: bool = False, enterprise: bool = False,
                     base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    env = {
        k: v for k, v in source.items()
        if not k.startswith(("WIRE_", "AGY_PROC_")) and k != "PYTHONHOME"
    }
    env["HOME"] = bridge_home
    env["AGY_CLI_CDE_AUTH_ACTION"] = action
    env["AGY_CDE_ENTERPRISE_AUTH"] = "true" if enterprise else "false"
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    env["GODEBUG"] = ("netdns=cgo," + env.get("GODEBUG", "")).rstrip(",")
    if isolated:
        env.update(_conv.ISOLATED_KEYRING_ENV)
    else:
        env.pop("WSL_INTEROP", None)
    if oob:
        env["SSH_TTY"] = "pyagy-isolated"
    else:
        for ssh_var in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"):
            env.pop(ssh_var, None)
    return env


def _has_incomplete_gcp_onboarding(token_path: str) -> bool:
    """Return True if ``token_path`` holds a ``gcp`` token missing ``user_tier`` and ``project_id``."""
    from wirecap.types import json_load_object

    try:
        with open(token_path, encoding="utf-8") as f:
            obj = json_load_object(f)
    except (OSError, ValueError):
        return False
    if obj.get("auth_method") != "gcp":
        return False
    return not bool(obj.get("user_tier") or obj.get("project_id"))


def _resolve_agy_bin(agy_bin: str | None = None) -> str:
    agy = agy_bin or _VENDOR_AGY
    if not os.path.isfile(agy):
        raise FileNotFoundError(
            f"the agy host is not built: {agy}; rebuild it with "
            "`python -m tasksolver.reconstruct --agy` (`pixi reinstall tasksolver` in a checkout)")
    return agy


def _store_mode(adir: str) -> AuthMode:
    if adir == _conv.GLOBAL_APP_DATA_DIR:
        return "global"
    if _conv.is_isolated_auth(adir):
        return "dedicated"
    return "shared"


def auth_status(app_data_dir: str | None = None, *,
                account: str | None = None,
                verify: bool = True,
                agy_bin: str | None = None,
                timeout: float = 15.0) -> AuthStatus:
    """Inspect the auth configuration of ``~/.gemini/<app_data_dir>`` without exposing tokens."""
    adir = _conv._validate_app_data_dir(_conv.resolve_app_data_dir(app_data_dir, account=account))
    root = _conv.store_root(adir)
    mode = _store_mode(adir)
    token_path = os.path.join(root, "antigravity-oauth-token")
    has_token_file = os.path.isfile(token_path) and not os.path.islink(token_path)
    project_id = _read_project_id(root)
    if not verify:
        return AuthStatus(
            app_data_dir=adir,
            store=root,
            mode=mode,
            has_token_file=has_token_file,
            project_id=project_id,
        )
    if mode == "dedicated" and not has_token_file:
        return AuthStatus(
            app_data_dir=adir,
            store=root,
            mode=mode,
            has_token_file=False,
            project_id=project_id,
            authenticated=False,
            email=None,
        )
    if mode == "dedicated" and has_token_file and _has_incomplete_gcp_onboarding(token_path):
        return AuthStatus(
            app_data_dir=adir,
            store=root,
            mode=mode,
            has_token_file=True,
            project_id=project_id,
            authenticated=False,
            email=None,
        )
    agy = _resolve_agy_bin(agy_bin)
    isolated = (mode == "dedicated")
    with _auth_action_bridge(root, isolated=isolated) as bridge_home:
        env = _auth_action_env(bridge_home, "check", isolated=isolated)
        proc = subprocess.run(
            [agy],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    combined = f"{proc.stdout}\n{proc.stderr}"
    match = _AUTH_EMAIL_RE.search(combined)
    return AuthStatus(
        app_data_dir=adir,
        store=root,
        mode=mode,
        has_token_file=has_token_file,
        project_id=_read_project_id(root),
        authenticated=(proc.returncode == 0),
        email=match.group(1) if match else None,
    )


def list_auth_stores(*, verify: bool = False, agy_bin: str | None = None) -> list[AuthStatus]:
    """List ``antigravity-cli``, ``pyagy``, and any ``pyagy-*`` profile directories under ``~/.gemini``."""
    gem = os.path.expanduser("~/.gemini")
    names: list[str] = [_conv.GLOBAL_APP_DATA_DIR, _conv.DEFAULT_APP_DATA_DIR]
    for path in sorted(glob.glob(os.path.join(gem, f"{_conv.DEFAULT_APP_DATA_DIR}-*"))):
        if os.path.isdir(path):
            base = os.path.basename(path)
            if base not in names:
                names.append(base)
    return [auth_status(name, verify=verify, agy_bin=agy_bin) for name in names]


def login(app_data_dir: str | None = None, *,
          account: str | None = None,
          switch: bool = False,
          oob: bool = False,
          enterprise: bool = False,
          agy_bin: str | None = None) -> int:
    """Run interactive Google OAuth sign-in for a dedicated ``pyagy`` store.

    Never touches ``~/.gemini/antigravity-cli`` or the OS D-Bus keyring. Pass ``switch=True``
    to clear previous credentials and project caches in the target store before logging in,
    ``oob=True`` to print a URL + read the pasted authorization code (recommended when
    signing into a different Google account in an incognito or secondary browser profile),
    and ``enterprise=False`` (default) for consumer Antigravity accounts.
    """
    agy = _resolve_agy_bin(agy_bin)
    root = enable_dedicated_auth(app_data_dir, account=account, clear_credentials=switch)
    flow = "out-of-band URL + code" if oob else "local browser redirect (pass --oob for URL + code paste)"
    print(f"pyagy dedicated login -> {root} [{flow}]", file=sys.stderr, flush=True)
    with _auth_action_bridge(root, isolated=True) as bridge_home:
        env = _auth_action_env(bridge_home, "login", isolated=True, oob=oob, enterprise=enterprise)
        completed = subprocess.run([agy], env=env)
    return completed.returncode


def _format_status(st: AuthStatus) -> str:
    auth_str = (
        "unverified" if st.authenticated is None
        else f"authenticated ({st.email})" if st.authenticated and st.email
        else "authenticated" if st.authenticated
        else "not logged in"
    )
    parts = [
        f"store:      {st.store}",
        f"app_dir:    {st.app_data_dir}",
        f"mode:       {st.mode}",
        f"token_file: {'present' if st.has_token_file else 'absent'}",
        f"project_id: {st.project_id or '-'}",
        f"status:     {auth_str}",
    ]
    return "\n".join(parts)


class _CLIArgs(argparse.Namespace):
    command: str | None
    account: str | None
    app_data_dir: str | None
    oob: bool
    enterprise: bool
    no_verify: bool
    verify: bool
    prepare_only: bool


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pyagy.auth",
        description="Manage dedicated file-backed Google authentication and accounts for pyagy / TaskSolver.",
    )
    sub = parser.add_subparsers(dest="command")

    def add_target(p: argparse.ArgumentParser) -> None:
        grp = p.add_mutually_exclusive_group()
        grp.add_argument("--account", help="Named pyagy account profile (e.g. 'alt' -> ~/.gemini/pyagy-alt)")
        grp.add_argument("--app-data-dir", help="Explicit store subdirectory under ~/.gemini (default: pyagy)")

    p_status = sub.add_parser("status", help="Show auth mode and logged-in account for the target store")
    add_target(p_status)
    p_status.add_argument("--no-verify", action="store_true", help="Inspect files only without invoking agy")

    p_list = sub.add_parser("list", help="List global and pyagy auth stores under ~/.gemini")
    p_list.add_argument("--verify", action="store_true", help="Verify active login email for each store")

    p_login = sub.add_parser("login", help="Enable dedicated auth and sign in with Google")
    add_target(p_login)
    p_login.add_argument("--oob", action="store_true",
                         help="Print OAuth URL and paste code in terminal (ideal for incognito / second account)")
    p_login.add_argument("--enterprise", action="store_true",
                         help="Use GCP Cloud Code enterprise OAuth instead of consumer Antigravity OAuth")

    p_switch = sub.add_parser("switch", help="Clear dedicated store credentials and sign into another account")
    add_target(p_switch)
    p_switch.add_argument("--oob", action="store_true",
                          help="Print OAuth URL and paste code in terminal (ideal for incognito / second account)")
    p_switch.add_argument("--enterprise", action="store_true",
                          help="Use GCP Cloud Code enterprise OAuth instead of consumer Antigravity OAuth")
    p_switch.add_argument("--prepare-only", action="store_true",
                          help="Clear and pin dedicated store without launching interactive sign-in yet")

    p_logout = sub.add_parser("logout", help="Clear dedicated credentials while staying isolated from global keyring")
    add_target(p_logout)

    p_shared = sub.add_parser("shared", help="Switch store back to sharing global ~/.gemini/antigravity-cli login")
    add_target(p_shared)

    ns = _CLIArgs()
    ns.command = None
    ns.account = None
    ns.app_data_dir = None
    ns.oob = False
    ns.enterprise = False
    ns.no_verify = False
    ns.verify = False
    ns.prepare_only = False
    args = parser.parse_args(argv, namespace=ns)

    cmd = args.command or "status"
    if cmd == "status":
        st = auth_status(args.app_data_dir, account=args.account, verify=not args.no_verify)
        print(_format_status(st))
        return 0
    if cmd == "list":
        for i, st in enumerate(list_auth_stores(verify=args.verify)):
            if i:
                print("---")
            print(_format_status(st))
        return 0
    if cmd == "login":
        return login(args.app_data_dir, account=args.account, switch=False,
                     oob=args.oob, enterprise=args.enterprise)
    if cmd == "switch":
        if args.prepare_only:
            root = enable_dedicated_auth(args.app_data_dir, account=args.account, clear_credentials=True)
            print(f"Prepared dedicated store (logged out, isolated from global keyring): {root}")
            return 0
        return login(args.app_data_dir, account=args.account, switch=True,
                     oob=args.oob, enterprise=args.enterprise)
    if cmd == "logout":
        root = logout(args.app_data_dir, account=args.account)
        print(f"Cleared dedicated credentials (still isolated from global keyring): {root}")
        return 0
    if cmd == "shared":
        root = use_shared_auth(args.app_data_dir, account=args.account)
        print(f"Restored shared global login for: {root}")
        return 0
    parser.error(f"Unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
