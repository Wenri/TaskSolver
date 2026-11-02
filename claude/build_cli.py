"""Build Claude's launcher, clean private Bun and bridged source/cache artifacts."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import platform
import runpy
import shutil
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
    value = cast(dict[str, object], manifest)["cli"]
    if not isinstance(value, dict):
        raise ValueError("Claude CLI pin must be an object")
    data = cast(dict[str, object], value)
    version, url, digest, size = data["version"], data["url"], data["sha256"], data["size"]
    if not isinstance(version, str) or not isinstance(url, str) or not isinstance(digest, str):
        raise ValueError("Claude CLI pin needs a string version, URL and SHA256")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("Claude CLI pin needs a positive integer size")
    return {"version": version, "url": url, "sha256": digest, "size": size}


def matches(path: Path, pin: CLIPin) -> bool:
    if not path.is_file() or path.stat().st_size != pin["size"]:
        return False
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == pin["sha256"]


def _download(target: Path, pin: CLIPin) -> Path:
    if matches(target, pin):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"[setup] downloading {target.name} (SHA256 verified)", flush=True)
    descriptor, name = tempfile.mkstemp(prefix=".claude-", dir=target.parent)
    temporary: Final = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with cast(HTTPResponse, urllib.request.urlopen(pin["url"], timeout=60)) as response:
                while block := response.read(1024 * 1024):
                    output.write(block)
        if not matches(temporary, pin):
            raise RuntimeError("Claude CLI download failed size/SHA256 verification")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def fetch_cli(root: Path, pin: CLIPin) -> Path:
    """Cache the pristine input separately; it is never a default launch target."""
    target = root / "build/cache" / f"claude-{pin['sha256']}"
    # Reuse the old verified input when upgrading a source checkout to this layout.
    previous = root / "vendor/sdk/src/claude_agent_sdk/_bundled/claude"
    if not target.exists() and matches(previous, pin):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(previous, target)
        target.chmod(0o600)
    return _download(target, pin)


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for name in ("build_cli.py", "unbundle.py", "native_bridge.mjs", "vendor.json",
                 "launcher.c", "native_cache.c", "cache.mjs"):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def _bundle_matches(directory: Path, fingerprint: str) -> bool:
    try:
        document = cast(object, json.loads((directory / "manifest.json").read_text()))
        if not isinstance(document, dict):
            return False
        manifest = cast(dict[str, object], document)
        if manifest.get("build_fingerprint") != fingerprint or manifest.get("native_bridge") is not True:
            return False
        hashes = manifest.get("code_sha256")
        if not isinstance(hashes, dict) or not hashes:
            return False
        code_hashes = cast(dict[object, object], hashes)
        if (manifest.get("format") != 3 or manifest.get("code_count") != len(code_hashes)
                or not {"tasksolver-entry.mjs", "tasksolver-resolver.mjs",
                        "tasksolver-native-bridge.mjs"}.issubset(code_hashes)):
            return False
        helpers = manifest.get("helper_sha256")
        if (not isinstance(helpers, dict)
                or set(cast(dict[object, object], helpers)) != {"libwire.node", "cache.mjs"}):
            return False
        cache_hashes = manifest.get("cache_sha256")
        if (not isinstance(cache_hashes, dict)
                or set(cast(dict[object, object], cache_hashes)) != {
                    name + ".jsc" for name in code_hashes
                    if isinstance(name, str) and not name.endswith(".cjs")}):
            return False
        metadata = manifest.get("module_metadata")
        if not isinstance(metadata, dict) or not metadata:
            return False
        expected_assets: set[str] = set()
        expected_modules: set[str] = set()
        for item in cast(dict[object, object], metadata).values():
            if not isinstance(item, dict):
                return False
            record = cast(dict[object, object], item)
            path, loader = record.get("path"), record.get("loader")
            if not isinstance(path, str):
                return False
            if loader == 1 and path.startswith("source/"):
                if path.removeprefix("source/") not in code_hashes:
                    return False
            elif loader in (5, 13) and path.startswith("assets/"):
                expected_assets.add(path.removeprefix("assets/"))
            elif loader == 10 and path.startswith("modules/"):
                expected_modules.add(path.removeprefix("modules/"))
            else:
                return False
        for key, expected_names in (("asset_sha256", expected_assets),
                                    ("native_module_sha256", expected_modules)):
            recorded = manifest.get(key)
            if not isinstance(recorded, dict) or set(cast(dict[object, object], recorded)) != expected_names:
                return False
        paths: dict[str, object] = {"claude": manifest.get("runtime_sha256"), "bun": manifest.get("bun_sha256")}
        for key, prefix in (("code_sha256", "source/"), ("cache_sha256", "source/"),
                            ("asset_sha256", "assets/"), ("native_module_sha256", "modules/"),
                            ("helper_sha256", "")):
            entries = manifest.get(key)
            if not isinstance(entries, dict) or not entries:
                return False
            for name, expected in cast(dict[object, object], entries).items():
                if not isinstance(name, str) or name.startswith("/") or ".." in Path(name).parts:
                    return False
                paths[prefix + name] = expected
        for name, expected in paths.items():
            path = directory / name
            if not isinstance(expected, str) or not path.is_file() or path.is_symlink():
                return False
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                    return False
        return all(os.access(directory / name, os.X_OK) for name in ("claude", "bun"))
    except (OSError, ValueError):
        return False


def build() -> Path:
    from claude.unbundle import unbundle

    root = Path(__file__).resolve().parent
    pin = _load_pin(root / "vendor.json")
    package = root / "vendor/sdk/src/claude_agent_sdk"
    sdk_version = cast(Mapping[str, object], runpy.run_path(str(package / "_cli_version.py")))["__cli_version__"]
    if sdk_version != pin["version"]:
        raise RuntimeError("Claude CLI pin does not match the vendored SDK")
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise RuntimeError("TaskSolver's bundled native runtimes require Linux x86-64")
    work = root / "build"
    work.mkdir(exist_ok=True)
    with (work / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bundled = package / "_bundled"
        fingerprint = _fingerprint(root)
        if _bundle_matches(bundled, fingerprint):
            return bundled / "claude"
        source = fetch_cli(root, pin)
        with tempfile.TemporaryDirectory(prefix=".bundled-", dir=package) as temporary:
            staging = Path(temporary) / "runtime"
            result = unbundle(staging, binary=source, pin=pin)
            document = cast(dict[str, object], json.loads(result.manifest.read_text()))
            document["build_fingerprint"] = fingerprint
            _ = result.manifest.write_text(json.dumps(document, indent=2) + "\n")
            ignore = bundled / ".gitignore"
            if ignore.is_file():
                shutil.copyfile(ignore, staging / ".gitignore")
            for path in staging.rglob("*"):
                path.chmod(0o755 if path.is_dir() or path in (result.runtime, result.bun) else 0o644)
            staging.chmod(0o755)
            previous = Path(temporary) / "previous"
            if bundled.exists():
                bundled.rename(previous)
            try:
                staging.rename(bundled)
            except BaseException:
                if previous.exists():
                    previous.rename(bundled)
                raise
        return bundled / "claude"


if __name__ == "__main__":
    print(build())
