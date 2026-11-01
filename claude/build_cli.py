"""Fetch the pinned Claude runtime without installing into the user's home."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import runpy
import tempfile
import urllib.request
from collections.abc import Mapping
from http.client import HTTPResponse
from pathlib import Path
from typing import Final, TypedDict, cast


class CLIPin(TypedDict):
    version: str
    url: str
    sha256: str
    size: int


def _load_pin(path: Path) -> CLIPin:
    manifest = cast(object, json.loads(path.read_text()))
    if not isinstance(manifest, dict):
        raise ValueError("Claude CLI manifest must be an object")
    cli = cast(dict[str, object], manifest)["cli"]
    if not isinstance(cli, dict):
        raise ValueError("Claude CLI pin must be an object")
    data = cast(dict[str, object], cli)
    version, url, digest, size = data["version"], data["url"], data["sha256"], data["size"]
    if not isinstance(version, str) or not isinstance(url, str) or not isinstance(digest, str):
        raise ValueError("Claude CLI pin needs string version, URL and SHA256")
    if not isinstance(size, int) or isinstance(size, bool):
        raise ValueError("Claude CLI pin needs an integer size")
    return {"version": version, "url": url, "sha256": digest, "size": size}


def matches(path: Path, pin: CLIPin) -> bool:
    if not path.is_file() or path.stat().st_size != pin["size"]:
        return False
    digest: Final = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == pin["sha256"]


def build() -> Path:
    root: Final = Path(__file__).resolve().parent
    pin: Final = _load_pin(root / "vendor.json")
    package: Final = root / "vendor/sdk/src/claude_agent_sdk"
    sdk_version: Final = cast(Mapping[str, object], runpy.run_path(str(package / "_cli_version.py")))["__cli_version__"]
    if sdk_version != pin["version"]:
        raise RuntimeError("Claude CLI pin does not match the vendored SDK")
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise RuntimeError("TaskSolver's bundled native runtimes require Linux x86-64")
    target: Final = package / "_bundled/claude"
    if matches(target, pin):
        target.chmod(0o755)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"[setup] downloading Claude Code {pin['version']} (SHA256 verified)", flush=True)
    descriptor, name = tempfile.mkstemp(prefix=".claude-", dir=target.parent)
    temporary: Final = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with cast(HTTPResponse, urllib.request.urlopen(pin["url"], timeout=60)) as response:
                while block := response.read(1024 * 1024):
                    output.write(block)
        if not matches(temporary, pin):
            raise RuntimeError("Claude CLI download failed size/SHA256 verification")
        temporary.chmod(0o755)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


if __name__ == "__main__":
    print(build())
