"""Rebuild the agy and Claude Code runtimes that redistributable TaskSolver bundles leave out.

Both runtimes are derived from proprietary upstream executables: agy's Go binary becomes the linked
host ``pyagy/vendor/agy`` with ``pyagy/vendor/libagy.so``, and Claude Code's binary becomes the
bridged runtime in ``claude_agent_sdk/_bundled/``. Local installs (the editable pixi workspace, a
wheel that uv or pip builds for a path dependency) build and keep them. Conda package builds
(``pixi publish``, conda source dependencies) are redistributable: setup.py neither fetches nor
ships the ``LOCAL_ONLY`` artifacts there.

This helper puts them back. It finds the pinned official executables on this machine (or takes
``--agy PATH`` / ``--claude PATH``), verifies them against the checkout's pins, rebuilds both
runtimes with the same steps as setup.py, and installs them into a target environment::

    pixi run reconstruct [--agy [PATH]] [--claude [PATH]] [--target ENV]

Run it from a TaskSolver checkout's pixi environment at the version installed in the target: the
environment supplies the native toolchain and the checkout supplies the build sources.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

# Package-relative artifacts built from proprietary upstream executables. Redistributable bundles
# never contain them (setup.py); this helper restores them.
LOCAL_ONLY: Final = ("pyagy/vendor/agy", "pyagy/vendor/libagy.so", "claude_agent_sdk/_bundled")

SDK_DIR: Final = Path("claude/vendor/sdk/src/claude_agent_sdk")
PYAGY_DIR: Final = Path("antigravity/pyagy")
# The Python mirror of the host's book table; it must match the host the checkout builds.
BOOKS: Final = Path("agy_process/books.py")

# Launchers small enough to scan for the absolute path of the executable they start: Nix
# makeBinaryWrapper ELFs (about 16 KB) and `exec` shell wrappers.
_LAUNCHER_MAX: Final = 1 << 20
_ABSOLUTE_PATH: Final = re.compile(rb"/[^\x00\s\"';:]+")


class ReconstructError(RuntimeError):
    """A precondition failed; the message says what to change."""


# ---- build steps shared with setup.py ------------------------------------------------------------

def cmake_python_args(env: Mapping[str, str]) -> list[str]:
    """Name the embedded interpreter on every configure. The CMake build dirs are shared with
    `pixi run build-shim`, and a PY3CONFIG cached from another env would embed that env's
    libpython (a different minor version after a Python bump)."""
    config = os.path.join(env.get("CONDA_PREFIX", ""), "bin", "python3-config")
    return [f"-DPY3CONFIG={config}"] if os.path.isfile(config) else []


def build_agy(source: str | os.PathLike[str], env: Mapping[str, str]) -> None:
    """Convert the pinned agy into antigravity/vendor/libagy.so and link the host vendor/agy.

    CMake + Ninja run the whole chain: configure verifies the raw agy cached in antigravity/build
    (fetching the pinned release only when the cache is absent); the build converts it, records
    metadata before patching, generates symbols_gen.h and links the host. Idempotent."""
    build = os.path.join("antigravity", "build")
    subprocess.run(["cmake", "-S", "antigravity", "-B", build, "-G", "Ninja", *cmake_python_args(env)],
                   cwd=source, env=env, check=True)
    subprocess.run(["cmake", "--build", build], cwd=source, env=env, check=True)


def build_claude(source: str | os.PathLike[str], env: Mapping[str, str]) -> None:
    """Stage Claude's launcher, clean private Bun, source, caches and assets together in
    claude_agent_sdk/_bundled from the executable cached in claude/build/cache (fetching the
    pinned release only when the cache is absent). Idempotent."""
    subprocess.run([sys.executable, "-m", "claude.build_cli"], cwd=source, env=env, check=True)


# ---- pinned inputs -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Pin:
    """A pinned upstream executable and where the checkout's build caches it."""

    runtime: str                 # "agy" or "claude"; also the command-line flag
    names: tuple[str, ...]       # file names a launcher's target may carry
    version: str
    size: int
    algorithm: str               # hashlib name of `digest`
    digest: str
    cache: Path
    mode: int                    # file mode of the cached copy


def _cli_pin(path: Path) -> dict[str, object]:
    document = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(document, dict):
        raise ReconstructError(f"{path} must hold a JSON object")
    cli = cast(dict[str, object], document).get("cli")
    if not isinstance(cli, dict):
        raise ReconstructError(f"{path} has no `cli` pin")
    return cast(dict[str, object], cli)


def _text(pin: Mapping[str, object], key: str, path: Path) -> str:
    value = pin.get(key)
    if not isinstance(value, str) or not value:
        raise ReconstructError(f"{path}: `cli.{key}` must be a non-empty string")
    return value


def _size(pin: Mapping[str, object], path: Path) -> int:
    value = pin.get("size")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReconstructError(f"{path}: `cli.size` must be a positive integer")
    return value


def agy_pin(source: Path) -> Pin:
    """antigravity/vendor.json: the extracted binary, cached under the archive's hash."""
    path = source / "antigravity" / "vendor.json"
    cli = _cli_pin(path)
    cache = source / "antigravity" / "build" / f"antigravity-{_text(cli, 'archive_sha512', path)}.raw"
    return Pin("agy", ("agy", "antigravity"), _text(cli, "version", path), _size(cli, path),
               "sha512", _text(cli, "sha512", path), cache, 0o755)


def claude_pin(source: Path) -> Pin:
    """claude/vendor.json: the executable, cached by claude/build_cli.py under its own hash."""
    path = source / "claude" / "vendor.json"
    cli = _cli_pin(path)
    digest = _text(cli, "sha256", path)
    return Pin("claude", ("claude",), _text(cli, "version", path), _size(cli, path), "sha256",
               digest, source / "claude" / "build" / "cache" / f"claude-{digest}", 0o600)


def mismatch(path: Path, pin: Pin) -> str | None:
    """Why ``path`` is not the pinned executable, or None when it is."""
    try:
        size = path.stat().st_size
        if size != pin.size:
            return f"size {size}, pinned {pin.size}"
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, pin.algorithm).hexdigest()
    except OSError as error:
        return error.strerror or type(error).__name__
    return None if digest == pin.digest else f"{pin.algorithm} differs from the pin (a modified copy)"


def expand(path: Path, names: Sequence[str]) -> list[Path]:
    """``path`` with symlinks resolved, then the executables it launches when it is a small
    launcher (Nix wrappers and exec scripts embed their target's absolute path) whose file name
    mentions one of ``names``."""
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            return []
        found = [resolved]
        if resolved.stat().st_size <= _LAUNCHER_MAX:
            for match in _ABSOLUTE_PATH.finditer(resolved.read_bytes()):
                target = Path(os.fsdecode(match.group()))
                if any(name in target.name for name in names) and target.is_file():
                    found.append(target.resolve())
    except OSError:
        return []
    return list(dict.fromkeys(found))


def _on_path(name: str) -> list[Path]:
    """Every executable ``name`` on PATH, in order (like ``which -a``)."""
    found: list[Path] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(entry or ".") / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            found.append(candidate)
    return found


def agy_candidates(pin: Pin, home: Path) -> list[Path]:
    """The checkout's cache, then where agy installs: PATH, the installer's ~/.local/bin (with the
    self-updater's agy.*.old backups, which may hold an older pinned release) and Nix profiles."""
    local = home / ".local" / "bin" / "agy"
    return [pin.cache, *_on_path("agy"), local, *sorted(local.parent.glob("agy.*.old")),
            home / ".nix-profile" / "bin" / "agy", Path("/usr/local/bin/agy")]


def claude_candidates(pin: Pin, home: Path) -> list[Path]:
    """The checkout's cache, then where Claude Code installs: PATH, the native installer's
    version store, the legacy local install, Nix profiles, and the native binaries shipped in
    editor extensions (VS Code, Cursor and their remote servers)."""
    versions = home / ".local" / "share" / "claude" / "versions"
    extensions = sorted(home.glob(".*/extensions/anthropic.claude-code-*-linux-x64"))
    return [pin.cache, *_on_path("claude"), versions / pin.version, *sorted(versions.glob("*")),
            home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude",
            home / ".nix-profile" / "bin" / "claude",
            *(extension / "resources" / "native-binary" / "claude" for extension in extensions)]


def select(pin: Pin, explicit: Path | None, candidates: Sequence[Path],
           report: Callable[[str], None]) -> Path:
    """``explicit`` (or the executable it launches) when it is the pinned build, else the first
    verified candidate; rejected candidates are reported with the reason."""
    rejected: list[str] = []
    seen: set[Path] = set()
    for candidate in ([explicit] if explicit is not None else candidates):
        for path in expand(candidate, pin.names):
            if path in seen:
                continue
            seen.add(path)
            reason = mismatch(path, pin)
            if reason is None:
                return path
            rejected.append(f"{path}: {reason}")
            if explicit is None:
                report(f"  skipped {path}: {reason}")
    if explicit is not None:
        detail = "; ".join(rejected) or "not found"
        raise ReconstructError(f"{explicit} is not the pinned {pin.runtime} {pin.version} build ({detail})")
    raise ReconstructError(f"found no pinned {pin.runtime} {pin.version} executable on this machine; "
                           f"pass --{pin.runtime} PATH")


def seed(pin: Pin, binary: Path) -> None:
    """Copy a verified executable to where the checkout's build expects the pinned input."""
    if binary == pin.cache.resolve():
        return
    pin.cache.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{pin.runtime}-", dir=pin.cache.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copyfile(binary, temporary)
        temporary.chmod(pin.mode)
        reason = mismatch(temporary, pin)
        if reason is not None:
            raise ReconstructError(f"{binary} changed while it was copied ({reason})")
        temporary.replace(pin.cache)
    finally:
        temporary.unlink(missing_ok=True)


# ---- the target environment ----------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """Where a target environment imports TaskSolver's runtime packages from."""

    label: str
    python: tuple[int, int] | None
    pyagy: Path | None
    claude_sdk: Path | None


# Locates packages without importing them (find_spec on a top-level name runs no package code).
_PROBE: Final = """\
import importlib.util, json, sys
def where(name):
    spec = importlib.util.find_spec(name)
    locations = list(spec.submodule_search_locations or []) if spec is not None else []
    return locations[0] if locations else None
print(json.dumps({"python": list(sys.version_info[:2]),
                  "pyagy": where("pyagy"), "claude_agent_sdk": where("claude_agent_sdk")}))
"""


def _probe(python: Path, label: str) -> Target:
    result = subprocess.run([os.fspath(python), "-I", "-c", _PROBE], capture_output=True, text=True,
                            timeout=120)
    if result.returncode != 0:
        raise ReconstructError(f"cannot inspect {python}: {result.stderr.strip() or result.returncode}")
    document = cast(object, json.loads(result.stdout))
    if not isinstance(document, dict):
        raise ReconstructError(f"unexpected probe output from {python}")
    data = cast(dict[str, object], document)
    version, pyagy, sdk = data.get("python"), data.get("pyagy"), data.get("claude_agent_sdk")
    if not isinstance(version, list):
        raise ReconstructError(f"unexpected probe output from {python}")
    numbers = [item for item in cast(list[object], version) if isinstance(item, int)]
    if len(numbers) != 2:
        raise ReconstructError(f"unexpected probe output from {python}")
    return Target(label, (numbers[0], numbers[1]), Path(pyagy) if isinstance(pyagy, str) else None,
                  Path(sdk) if isinstance(sdk, str) else None)


def _python_from_path(path: Path) -> tuple[int, int] | None:
    """The version in a ``lib/pythonX.Y/site-packages`` path, if any."""
    for part in path.parts:
        major, dot, minor = part.removeprefix("python").partition(".")
        if part.startswith("python") and dot and major.isdigit() and minor.isdigit():
            return int(major), int(minor)
    return None


def resolve_target(target: str | None) -> Target:
    """An environment prefix, a Python executable or a site-packages directory; by default the
    environment running this helper."""
    if target is None:
        return _probe(Path(sys.executable), "this environment")
    path = Path(target).expanduser().absolute()
    if path.is_dir():
        for python in (path / "bin" / "python3", path / "bin" / "python"):
            if python.is_file():
                return _probe(python, str(path))
        pyagy, sdk = path / "pyagy", path / "claude_agent_sdk"
        if pyagy.is_dir() or sdk.is_dir():
            return Target(str(path), _python_from_path(path), pyagy if pyagy.is_dir() else None,
                          sdk if sdk.is_dir() else None)
        raise ReconstructError(f"{path} is neither an environment prefix nor a site-packages directory")
    if path.is_file():
        return _probe(path, str(path))
    raise ReconstructError(f"target {path} does not exist")


def sdk_version(sdk: Path) -> str | None:
    """``__version__`` of a claude_agent_sdk package, read without importing it."""
    try:
        tree = ast.parse((sdk / "_version.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and any(isinstance(name, ast.Name) and name.id == "__version__" for name in node.targets)):
            return node.value.value
    return None


def _digest(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None


def _replace_file(source: Path, destination: Path, mode: int) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}-", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(mode)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def install_agy(source: Path, pyagy: Path) -> None:
    """Copy the built library and host into ``pyagy/vendor``, where a local wheel ships them."""
    built = source / "antigravity" / "vendor"
    vendor = pyagy / "vendor"
    vendor.mkdir(exist_ok=True)
    for name in ("libagy.so", "agy"):            # the host needs the library to start
        _replace_file(built / name, vendor / name, 0o755)
        if _digest(vendor / name) != _digest(built / name):
            raise ReconstructError(f"{vendor / name} does not match the build after copying")


def install_claude(source: Path, sdk: Path, valid: Callable[[Path], bool]) -> None:
    """Replace ``claude_agent_sdk/_bundled`` with the checkout's runtime, verified before the
    swap; the previous runtime is restored if the swap fails."""
    built = source / SDK_DIR / "_bundled"
    destination = sdk / "_bundled"
    staging = Path(tempfile.mkdtemp(prefix=".bundled-", dir=sdk))
    try:
        runtime = staging / "runtime"
        _ = shutil.copytree(built, runtime, symlinks=True)
        if not valid(runtime):
            raise ReconstructError(f"the Claude runtime copied from {built} failed verification")
        previous = staging / "previous"
        if destination.exists():
            destination.rename(previous)
        try:
            runtime.rename(destination)
        except BaseException:
            if previous.exists():
                previous.rename(destination)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def claude_validator(source: Path) -> Callable[[Path], bool]:
    """claude/build_cli.py's own bundle check against this checkout's build fingerprint."""
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    from claude.build_cli import _bundle_matches, _fingerprint

    fingerprint = _fingerprint(source / "claude")
    return lambda directory: _bundle_matches(directory, fingerprint)


# ---- command line --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Job:
    """One runtime to restore: its pinned input, the target package and how to build/install."""

    pin: Pin
    package: Path
    in_place: bool                   # the target imports this package from the checkout itself
    explicit: Path | None
    candidates: Callable[[], list[Path]]
    build: Callable[[Path, Mapping[str, str]], None]
    install: Callable[[Path, Path], None]


def find_source(explicit: str | None) -> Path:
    """The TaskSolver checkout to build in: ``explicit``, else the one this module runs from."""
    root = Path(explicit).expanduser() if explicit else Path(__file__).resolve().parents[1]
    if (root / "antigravity" / "CMakeLists.txt").is_file() and (root / "claude" / "build_cli.py").is_file():
        return root.resolve()
    if explicit:
        raise ReconstructError(f"{root} is not a TaskSolver checkout")
    raise ReconstructError(
        "this tasksolver is installed without its sources; run the helper from a TaskSolver checkout "
        f"at the same version:\n  pixi run -m <checkout>/pyproject.toml reconstruct --target {sys.prefix}")


def _require_tools(runtime: str, tools: Sequence[str]) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise ReconstructError(f"rebuilding {runtime} needs {', '.join(missing)} on PATH; run the helper "
                               "from the TaskSolver pixi environment (pixi run reconstruct ...)")


def agy_job(source: Path, target: Target, explicit: Path | None) -> Job:
    if target.pyagy is None:
        raise ReconstructError(f"{target.label} has no pyagy package; install tasksolver there first")
    checkout = source / PYAGY_DIR
    in_place = target.pyagy.resolve() == checkout.resolve()
    if not in_place and _digest(target.pyagy / BOOKS) != _digest(checkout / BOOKS):
        raise ReconstructError(f"{target.pyagy} comes from another TaskSolver version (its agy book table "
                               "differs); use a checkout at the installed version")
    _require_tools("agy", ("cmake", "ninja"))
    pin = agy_pin(source)
    return Job(pin, target.pyagy, in_place, explicit, lambda: agy_candidates(pin, Path.home()),
               build_agy, install_agy)


def claude_job(source: Path, target: Target, explicit: Path | None) -> Job:
    if target.claude_sdk is None:
        raise ReconstructError(f"{target.label} has no claude_agent_sdk package; install tasksolver there first")
    pin = claude_pin(source)
    in_place = target.claude_sdk.resolve() == (source / SDK_DIR).resolve()
    installed, checkout = sdk_version(target.claude_sdk), sdk_version(source / SDK_DIR)
    if not in_place and installed != checkout:
        raise ReconstructError(f"{target.claude_sdk} is Agent SDK {installed}, the checkout's is "
                               f"{checkout}; use a checkout at the installed version")
    _require_tools("claude", ("bun_graph",))
    return Job(pin, target.claude_sdk, in_place, explicit, lambda: claude_candidates(pin, Path.home()),
               build_claude,
               lambda checkout, sdk: install_claude(checkout, sdk, claude_validator(checkout)))


def _say(message: str) -> None:
    print(f"[reconstruct] {message}", file=sys.stderr, flush=True)


class _Arguments(argparse.Namespace):
    agy: str | None = None
    claude: str | None = None
    target: str | None = None
    source: str | None = None


def _explicit(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tasksolver.reconstruct",
        description="Rebuild the agy and Claude Code runtimes from the pinned official executables "
                    "on this machine and install them into a TaskSolver environment.")
    _ = parser.add_argument("--agy", nargs="?", const="", metavar="PATH",
                            help="restore agy, from PATH or an auto-detected install")
    _ = parser.add_argument("--claude", nargs="?", const="", metavar="PATH",
                            help="restore Claude Code, from PATH or an auto-detected install")
    _ = parser.add_argument("--target", metavar="ENV",
                            help="environment prefix, Python executable or site-packages directory "
                                 "(default: this environment)")
    _ = parser.add_argument("--source", metavar="CHECKOUT",
                            help="TaskSolver checkout to build in (default: the one running this module)")
    args = parser.parse_args(argv, namespace=_Arguments())
    both = args.agy is None and args.claude is None
    try:
        source = find_source(args.source)
        target = resolve_target(args.target)
        if not os.environ.get("CONDA_PREFIX"):
            raise ReconstructError("CONDA_PREFIX is not set; run the helper inside the TaskSolver pixi "
                                   "environment (pixi run reconstruct ...)")
        jobs: list[Job] = []
        if both or args.agy is not None:
            jobs.append(agy_job(source, target, _explicit(args.agy)))
        if both or args.claude is not None:
            jobs.append(claude_job(source, target, _explicit(args.claude)))
        expected = (sys.version_info.major, sys.version_info.minor)
        found = target.python
        if any(not job.in_place for job in jobs) and found is not None and found != expected:
            raise ReconstructError(f"{target.label} runs Python {found[0]}.{found[1]}, but the runtimes "
                                   f"embed Python {expected[0]}.{expected[1]}")
        for job in jobs:                         # every input first: fail before any build
            _say(f"{job.pin.runtime} {job.pin.version}: looking for the pinned executable")
            binary = select(job.pin, job.explicit, job.candidates(), _say)
            _say(f"{job.pin.runtime}: using {binary}")
            seed(job.pin, binary)
        env = dict(os.environ)
        for job in jobs:
            job.build(source, env)
            if job.in_place:
                _say(f"{job.pin.runtime}: rebuilt in place ({target.label} imports it from {source})")
            else:
                job.install(source, job.package)
                _say(f"{job.pin.runtime}: installed into {job.package}")
    except (ReconstructError, OSError, ValueError, subprocess.SubprocessError) as error:
        _say(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
