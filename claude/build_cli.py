"""Build Claude's launcher, clean private Bun and bridged source/cache artifacts."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import platform
import runpy
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Mapping
from http.client import HTTPResponse
from pathlib import Path
from typing import Final, TypedDict, cast


class DownloadPin(TypedDict):
    url: str
    sha256: str
    size: int


class CLIPin(DownloadPin):
    version: str


class ToolPin(DownloadPin):
    revision: str


def _manifest_section(path: Path, key: str) -> dict[str, object]:
    manifest = cast(object, json.loads(path.read_text()))
    if not isinstance(manifest, dict):
        raise ValueError("Claude CLI manifest must be an object")
    cli = cast(dict[str, object], manifest)[key]
    if not isinstance(cli, dict):
        raise ValueError("Claude CLI pin must be an object")
    return cast(dict[str, object], cli)


def _download_pin(data: Mapping[str, object]) -> DownloadPin:
    url, digest, size = data["url"], data["sha256"], data["size"]
    if not isinstance(url, str) or not isinstance(digest, str):
        raise ValueError("Download pin needs a string URL and SHA256")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("Download pin needs a positive integer size")
    return {"url": url, "sha256": digest, "size": size}


def _load_pin(path: Path) -> CLIPin:
    data = _manifest_section(path, "cli")
    version = data["version"]
    if not isinstance(version, str):
        raise ValueError("Claude CLI pin needs a string version")
    return {**_download_pin(data), "version": version}


def _load_tool_pin(path: Path) -> ToolPin:
    data = _manifest_section(path, "bun_graph")
    revision = data["revision"]
    if not isinstance(revision, str) or len(revision) != 40 or any(
            character not in "0123456789abcdef" for character in revision):
        raise ValueError("bun_graph pin needs a full Git revision")
    return {**_download_pin(data), "revision": revision}


def matches(path: Path, pin: DownloadPin) -> bool:
    if not path.is_file() or path.stat().st_size != pin["size"]:
        return False
    digest: Final = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == pin["sha256"]


def _download(target: Path, pin: DownloadPin) -> Path:
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


def build_bun_graph(root: Path) -> Path:
    """Compile upstream's pinned extractor with the host C++ toolchain."""
    pin = _load_tool_pin(root / "vendor.json")
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    if not compiler:
        raise ValueError("CXX must name a compiler")
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "source-highlight"], text=True))
    lib_dir = subprocess.check_output(
        ["pkg-config", "--variable=libdir", "source-highlight"], text=True).strip()
    data_dir = subprocess.check_output(
        ["pkg-config", "--variable=pkgdatadir", "source-highlight"], text=True).strip()
    if not data_dir:
        prefix = subprocess.check_output(
            ["pkg-config", "--variable=prefix", "source-highlight"], text=True).strip()
        data_dir = str(Path(prefix) / "share/source-highlight")
    if not (Path(data_dir) / "javascript.lang").is_file():
        raise RuntimeError("source-highlight JavaScript definitions are unavailable")
    compiler_version = subprocess.check_output([*compiler, "--version"], text=True)
    identity = json.dumps([2, pin, compiler, compiler_version, flags, data_dir, lib_dir], sort_keys=True)
    key = hashlib.sha256(identity.encode()).hexdigest()[:20]
    target = root / "build/tools" / f"bun_graph-{key}"
    if target.is_file() and os.access(target, os.X_OK):
        return target
    archive = _download(root / "build/cache" / f"patchnar-{pin['revision']}.tar.gz", pin)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bun-graph-", dir=target.parent) as temporary:
        directory = Path(temporary)
        files = ("bun_graph.cc", "patchelf.cc", "patchelf.h", "elf.h", "source_patcher.cc", "source_patcher.h")
        with tarfile.open(archive, "r:gz") as source:
            for name in files:
                member = source.getmember(f"patchnar-{pin['revision']}/src/{name}")
                if not member.isfile():
                    raise ValueError("Unexpected non-file in pinned bun_graph sources")
                contents = source.extractfile(member)
                if contents is None:
                    raise ValueError("Missing bun_graph source contents")
                with contents, (directory / name).open("wb") as output:
                    shutil.copyfileobj(contents, output)
        candidate = directory / "bun_graph"
        # 3.1.9's formatter headers still use dynamic exception specifications.
        # Its upstream adapter needs only C++14; the ELF parser needs C++23.
        adapter = directory / "source_patcher.o"
        subprocess.run([*compiler, "-std=c++14", "-O2",
                        f"-DSOURCE_HIGHLIGHT_DATA_DIR={json.dumps(data_dir)}",
                        *flags, "-c", str(directory / "source_patcher.cc"), "-o", str(adapter)], check=True)
        subprocess.run([*compiler, "-std=c++23", "-O2", "-DPATCHELF_AS_LIBRARY",
                        "-D_FILE_OFFSET_BITS=64",
                        str(directory / "bun_graph.cc"), str(directory / "patchelf.cc"),
                        str(adapter), *flags, f"-Wl,-rpath,{lib_dir}", "-o", str(candidate)], check=True)
        candidate.replace(target)
    return target


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
        tool = build_bun_graph(root)
        with tempfile.TemporaryDirectory(prefix=".bundled-", dir=package) as temporary:
            staging = Path(temporary) / "runtime"
            result = unbundle(staging, binary=source, bun_graph=str(tool), pin=pin)
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
