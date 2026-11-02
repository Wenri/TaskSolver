"""Locate the packaged Claude runtime without the SDK's global-CLI fallback."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from wirecap.types import json_loads_object

# Redistributable bundles leave the runtime out (it derives from the proprietary Claude Code
# executable); the reconstruct helper rebuilds it from the pinned official executable.
_REBUILD = ("rebuild it with `python -m tasksolver.reconstruct --claude` "
            "(`pixi reinstall tasksolver` in a checkout)")


def bundled_cli() -> str:
    """Require the executable and its bridged source graph; build tools verify hashes."""
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or spec.origin is None:
        raise FileNotFoundError(f"The bundled Claude SDK is missing; {_REBUILD}")
    directory = Path(spec.origin).resolve().parent / "_bundled"
    binary = directory / "claude"
    required = (
        binary, directory / "bun", directory / "manifest.json", directory / "source/tasksolver-entry.mjs",
        directory / "source/tasksolver-resolver.mjs", directory / "source/tasksolver-native-bridge.mjs",
        directory / "source/root/cli",
    )
    for item in required:
        if not item.is_file():
            raise FileNotFoundError(f"Bundled Claude runtime is incomplete: {item}; {_REBUILD}")
    for executable in (binary, directory / "bun"):
        if not os.access(executable, os.X_OK):
            raise PermissionError(f"Bundled Claude runtime is not executable: {executable}; {_REBUILD}")
    try:
        manifest = json_loads_object((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError(f"Bundled Claude manifest is invalid; {_REBUILD}") from error
    if manifest.get("format") != 3 or manifest.get("native_bridge") is not True:
        raise RuntimeError(f"Bundled Claude runtime is not bridged; {_REBUILD}")
    return os.fspath(binary.resolve())
