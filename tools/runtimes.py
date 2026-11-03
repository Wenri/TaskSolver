#!/usr/bin/env python3
"""Track, adopt and release upstream versions of TaskSolver's four agent runtimes.

    python3 tools/runtimes.py check [RUNTIME ...] [--github-output FILE [--held FILE]]
    python3 tools/runtimes.py title TARGETS
    python3 tools/runtimes.py adopt RUNTIME [--version VERSION] [--cli-version VERSION]
    python3 tools/runtimes.py docs [--check]
    python3 tools/runtimes.py bump
    python3 tools/runtimes.py notes [RUNTIME ...]

Each runtime has one pin file. antigravity/vendor.json pins agy's release archive and the binary
inside it; claude/vendor.json pins the Agent SDK tag and the Claude Code executable, which are
released and tracked separately (the executable is never older than the one the SDK bundles);
codex/vendor.json and kimi/vendor.json pin the upstream tag and commit of their vendored trees,
whose single tag carries both the CLI and its SDK (Codex's Python SDK, Kimi Code's node SDK).

A vendored tree is its upstream tag plus TaskSolver's patch. Adoption replays that patch onto the
new release with a three-way merge of trees (base: the pinned tag, ours: the committed tree,
theirs: the new tag). Conflicted files keep their markers and the adoption fails naming them; a
lock file that the build regenerates (codex's Cargo.lock) takes the new release's copy instead.

Standard library only (Python >= 3.12), so the workflow's check job needs no pixi environment.
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
import tarfile
import tempfile
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPResponse
from pathlib import Path
from typing import Final, cast

ROOT = Path(__file__).resolve().parents[1]
RUNTIMES: Final = ("agy", "codex", "claude", "kimi")
AGY_MANIFEST: Final = os.environ.get(
    "AGY_MANIFEST_BASE", "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
) + "/manifests/linux_amd64.json"
CLAUDE_RELEASES: Final = "https://downloads.claude.ai/claude-code-releases"
# What `check` compares: each runtime's separately released parts.
COMPONENTS: Final = {"agy": ("agy",), "codex": ("codex",), "claude": ("claude-sdk", "claude-cli"),
                     "kimi": ("kimi",)}
LATEST: Final = {
    "codex": "https://api.github.com/repos/openai/codex/releases/latest",
    "claude": "https://api.github.com/repos/anthropics/claude-agent-sdk-python/releases/latest",
    "kimi": "https://registry.npmjs.org/@moonshot-ai%2Fkimi-code/latest",
}
# The merge's synthetic commits need an identity even where git has none configured.
_IDENTITY: Final = {"GIT_AUTHOR_NAME": "tasksolver", "GIT_AUTHOR_EMAIL": "tasksolver@localhost",
                    "GIT_COMMITTER_NAME": "tasksolver", "GIT_COMMITTER_EMAIL": "tasksolver@localhost"}


class AdoptionError(RuntimeError):
    """A step failed; the message names what needs hand work."""


@dataclass(frozen=True)
class Tree:
    """A vendored upstream tree, its pin file and how its release tags are named."""

    pin: str                        # pin file, relative to the repository root
    section: str                    # object in the pin file holding repository, tag and commit
    directory: str                  # the vendored tree, relative to the repository root
    tag_prefix: str                 # release tag = tag_prefix + version
    regenerated: tuple[str, ...]    # tree-relative lock files the build rewrites: take theirs


TREES: Final = {
    "codex": Tree("codex/vendor.json", "upstream", "codex/vendor", "rust-v", ("codex-rs/Cargo.lock",)),
    "claude": Tree("claude/vendor.json", "sdk", "claude/vendor/sdk", "v", ()),
    "kimi": Tree("kimi/vendor.json", "upstream", "kimi/vendor/kimi-code", "@moonshot-ai/kimi-code@", ()),
}

# Pinned versions written into prose and code: (file, line pattern, replacement template).
_DOCS: Final = (
    ("README.md", r"^\| Antigravity \(`agy`\) \| \*\*[^*|]+\*\* \|", "| Antigravity (`agy`) | **{agy}** |"),
    ("README.md", r"^\| Codex \| \*\*[^*|]+\*\* \(`[^`|]+`\) \|", "| Codex | **{codex}** (`rust-v{codex}`) |"),
    ("README.md", r"^\| Kimi Code \| \*\*[^*|]+\*\* \(`[^`|]+`\) \|",
     "| Kimi Code | **{kimi}** (`@moonshot-ai/kimi-code@{kimi}`) |"),
    ("README.md", r"^\| Claude Code \| \*\*[^*|]+\*\*, with Agent SDK \*\*[^*|]+\*\* \|",
     "| Claude Code | **{cli}**, with Agent SDK **{sdk}** |"),
    ("codex/pycodex/sdk.py", r'client_version="[^"]+"', 'client_version="{codex}"'),
)


# ---- JSON, HTTP and git --------------------------------------------------------------------------

def _load(path: Path) -> dict[str, object]:
    document = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(document, dict):
        raise AdoptionError(f"{path} must hold a JSON object")
    return cast(dict[str, object], document)


def _save(path: Path, document: Mapping[str, object]) -> None:
    _ = path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _object(document: Mapping[str, object], key: str, where: object) -> dict[str, object]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise AdoptionError(f"{where}: `{key}` must be an object")
    return cast(dict[str, object], value)


def _text(document: Mapping[str, object], key: str, where: object) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise AdoptionError(f"{where}: `{key}` must be a non-empty string")
    return value


def _integer(document: Mapping[str, object], key: str, where: object) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AdoptionError(f"{where}: `{key}` must be a positive integer")
    return value


def _open(url: str) -> HTTPResponse:
    headers = {"User-Agent": "tasksolver-runtime-updates"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers |= {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    request = urllib.request.Request(url, headers=headers)
    return cast(HTTPResponse, urllib.request.urlopen(request, timeout=120))   # noqa: S310 (fixed https hosts)


def _get(url: str) -> dict[str, object]:
    with _open(url) as response:
        document = cast(object, json.load(response))
    if not isinstance(document, dict):
        raise AdoptionError(f"{url} did not return a JSON object")
    return cast(dict[str, object], document)


def _download(url: str, target: Path) -> None:
    with _open(url) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output, 1 << 20)


def _digest(path: Path, algorithm: str) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def _git(*args: str, env: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                            env=None if env is None else {**os.environ, **env})
    if result.returncode != 0:
        raise AdoptionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


# ---- versions ------------------------------------------------------------------------------------

def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in cast(list[str], re.findall(r"\d+", version)))


def current(component: str) -> str:
    """The pinned version of a component (a runtime name means its main part: claude's SDK)."""
    if component == "agy":
        path = ROOT / "antigravity" / "vendor.json"
        return _text(_object(_load(path), "cli", path), "version", path)
    if component == "claude-cli":
        path = ROOT / TREES["claude"].pin
        return _text(_object(_load(path), "cli", path), "version", path)
    tree = TREES[component.removesuffix("-sdk")]
    path = ROOT / tree.pin
    return _text(_object(_load(path), tree.section, path), "tag", path).removeprefix(tree.tag_prefix)


def latest(component: str) -> str:
    """The newest upstream release of a component (stable releases; Claude Code's latest channel)."""
    if component == "agy":
        return _text(_get(AGY_MANIFEST), "version", AGY_MANIFEST)
    if component == "claude-cli":
        with _open(f"{CLAUDE_RELEASES}/latest") as response:
            release = response.read().decode().strip()
        if not release or not release[0].isdigit():
            raise AdoptionError(f"unexpected Claude Code release {release!r}")
        return release
    runtime = component.removesuffix("-sdk")
    url = LATEST[runtime]
    document = _get(url)
    if runtime == "kimi":
        return _text(document, "version", url)
    tag, prefix = _text(document, "tag_name", url), TREES[runtime].tag_prefix
    if not tag.startswith(prefix):
        raise AdoptionError(f"{runtime}'s latest release {tag} is not a {prefix}* tag")
    return tag.removeprefix(prefix)


@dataclass(frozen=True)
class Status:
    name: str           # a component: agy, codex, claude-sdk, claude-cli or kimi
    current: str
    latest: str

    @property
    def runtime(self) -> str:
        return self.name.split("-")[0]

    @property
    def outdated(self) -> bool:
        return _key(self.latest) > _key(self.current)

    @property
    def target(self) -> str:
        return f"{self.name} {self.latest}"


# The workflow opens one issue titled HAND_WORK + targets when an adoption needs hand work.
HAND_WORK: Final = "Runtime update needs hand work: "


def hand_work_title(targets: str) -> str:
    return HAND_WORK + targets


def held_targets(titles: Iterable[str]) -> set[str]:
    """The `component version` targets that open hand-work issues track."""
    return {target.strip() for title in titles if title.startswith(HAND_WORK)
            for target in title.removeprefix(HAND_WORK).split(",") if target.strip()}


def adoptable(outdated: Sequence[Status], held: set[str]) -> list[Status]:
    """The outdated components to adopt now: each runtime's, unless issues hold all its targets.

    A held runtime is retried once upstream moves past the versions its issue names.
    """
    runtimes = {status.runtime for status in outdated}
    held_runtimes = {runtime for runtime in runtimes
                     if {status.target for status in outdated if status.runtime == runtime} <= held}
    return [status for status in outdated if status.runtime not in held_runtimes]


def _claude_cli_version(sdk: Path) -> str:
    """``__cli_version__`` of a vendored Agent SDK, read without importing it."""
    source = sdk / "src" / "claude_agent_sdk" / "_cli_version.py"
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and any(isinstance(name, ast.Name) and name.id == "__cli_version__" for name in node.targets)):
            return node.value.value
    raise AdoptionError(f"{source} names no Claude Code version")


def versions() -> dict[str, str]:
    """Every pinned version, keyed as the documentation templates use them."""
    claude = ROOT / TREES["claude"].pin
    return {"agy": current("agy"), "codex": current("codex"), "kimi": current("kimi"),
            "sdk": current("claude-sdk"), "cli": _text(_object(_load(claude), "cli", claude), "version", claude)}


# ---- adoption ------------------------------------------------------------------------------------

def adopt_agy(version: str | None) -> str:
    """Pin agy's current release: its archive, and the extracted binary that CMake converts."""
    manifest = _get(AGY_MANIFEST)
    release = _text(manifest, "version", AGY_MANIFEST)
    if version is not None and version != release:
        raise AdoptionError(f"agy's release manifest serves only {release}, not {version}")
    url, archive_sha512 = _text(manifest, "url", AGY_MANIFEST), _text(manifest, "sha512", AGY_MANIFEST)
    cache = ROOT / "antigravity" / "build" / f"antigravity-{archive_sha512}.raw"
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache.parent) as directory:
        archive, binary = Path(directory) / "agy.tar.gz", Path(directory) / "antigravity"
        _download(url, archive)
        if _digest(archive, "sha512") != archive_sha512:
            raise AdoptionError(f"agy {release}: the archive does not match its manifest's sha512")
        with tarfile.open(archive) as bundle:
            member = bundle.extractfile("antigravity")
            if member is None:
                raise AdoptionError(f"agy {release}: the archive has no `antigravity` executable")
            with member, binary.open("wb") as output:
                shutil.copyfileobj(member, output, 1 << 20)
        binary.chmod(0o755)
        pin = {"version": release, "url": url, "archive_sha512": archive_sha512,
               "sha512": _digest(binary, "sha512"), "size": binary.stat().st_size}
        binary.replace(cache)            # the input CMake would otherwise fetch and extract
    _save(ROOT / "antigravity" / "vendor.json", {"cli": pin})
    return release


def _fetch(repository: str, tag: str) -> str:
    """The commit a release tag names, fetched shallowly into this repository's object store."""
    _ = _git("fetch", "--quiet", "--depth=1", "--no-tags", repository, f"refs/tags/{tag}")
    return _git("rev-parse", "FETCH_HEAD^{commit}")


def _commit(tree: str, *parents: str) -> str:
    # Throwaway merge inputs: never signed, whatever commit.gpgSign says.
    arguments = ["commit-tree", "--no-gpg-sign", tree, "-m", "tasksolver adoption"]
    for parent in parents:
        arguments += ["-p", parent]
    return _git(*arguments, env=_IDENTITY)


def _take(merged: str, theirs: str, paths: Sequence[str]) -> str:
    """``merged`` with ``paths`` replaced by their copies in the tree ``theirs``."""
    with tempfile.TemporaryDirectory() as directory:
        index = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        _ = _git("read-tree", merged, env=index)
        for path in paths:
            entry = _git("ls-tree", theirs, "--", path)
            if entry:
                mode, _kind, rest = entry.split(" ", 2)
                _ = _git("update-index", "--add", "--cacheinfo", f"{mode},{rest.split(chr(9), 1)[0]},{path}",
                         env=index)
            else:
                _ = _git("update-index", "--force-remove", "--", path, env=index)
        return _git("write-tree", env=index)


def merge(tree: Tree, base: str, theirs: str) -> tuple[str, list[str]]:
    """Replay the committed tree's changes since commit ``base`` onto commit ``theirs``.

    Returns the merged tree and the files left in conflict (they carry conflict markers)."""
    root = _commit(_git("rev-parse", f"{base}^{{tree}}"))
    ours = _commit(_git("rev-parse", f"HEAD:{tree.directory}"), root)
    theirs_tree = _git("rev-parse", f"{theirs}^{{tree}}")
    new = _commit(theirs_tree, root)
    result = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", ours, new],
                            cwd=ROOT, capture_output=True, text=True)
    lines = result.stdout.splitlines()
    if result.returncode not in (0, 1) or not lines:
        raise AdoptionError(f"git merge-tree failed: {result.stderr.strip()}")
    merged, conflicts = lines[0], sorted({line for line in lines[1:] if line})
    regenerated = [path for path in conflicts if path in tree.regenerated]
    if regenerated:
        merged = _take(merged, theirs_tree, regenerated)
    return merged, [path for path in conflicts if path not in tree.regenerated]


def _checkout(directory: str, tree: str) -> None:
    """Replace ``directory`` in the index and work tree with ``tree``; ignored files stay."""
    _ = _git("rm", "-r", "-q", "--", directory)
    _ = _git("read-tree", f"--prefix={directory}/", "-u", tree)


def _claude_cli(version: str) -> dict[str, object]:
    """The pin of a Claude Code release, from Anthropic's release manifest."""
    url = f"{CLAUDE_RELEASES}/{version}/manifest.json"
    platform = _object(_object(_get(url), "platforms", url), "linux-x64", url)
    return {"version": version, "url": f"{CLAUDE_RELEASES}/{version}/linux-x64/claude",
            "sha256": _text(platform, "checksum", url), "size": _integer(platform, "size", url)}


def adopt_tree(runtime: str, version: str | None) -> tuple[str, list[str]]:
    """Move a vendored tree and its pin to ``version``; returns it and any conflicted files."""
    tree = TREES[runtime]
    path = ROOT / tree.pin
    document = _load(path)
    upstream = _object(document, tree.section, path)
    repository, old_tag = _text(upstream, "repository", path), _text(upstream, "tag", path)
    release = version or latest(runtime)
    new_tag = tree.tag_prefix + release
    if new_tag == old_tag:
        raise AdoptionError(f"{runtime} is already at {release}")
    if _git("status", "--porcelain", "--", tree.directory):
        raise AdoptionError(f"{tree.directory} has uncommitted changes; commit or stash them first")
    base, theirs = _fetch(repository, old_tag), _fetch(repository, new_tag)
    merged, conflicts = merge(tree, base, theirs)
    _checkout(tree.directory, merged)
    upstream |= {"tag": new_tag, "commit": theirs}
    _save(path, document)
    return release, conflicts


def adopt_claude(sdk: str | None, cli: str | None) -> tuple[str, list[str]]:
    """Move the Agent SDK and the Claude Code pin, each to its own release (default: the latest).

    The executable stays at least as new as the one the SDK bundles."""
    pinned_sdk, pinned_cli = current("claude-sdk"), current("claude-cli")
    sdk_release, conflicts = sdk or latest("claude-sdk"), list[str]()
    if sdk_release != pinned_sdk:
        _, conflicts = adopt_tree("claude", sdk_release)
        if conflicts:
            return f"SDK {sdk_release}", conflicts
    bundled = _claude_cli_version(ROOT / TREES["claude"].directory)
    cli_release = cli or max(latest("claude-cli"), bundled, key=_key)
    if _key(cli_release) < _key(bundled):
        raise AdoptionError(f"Claude Code {cli_release} is older than the {bundled} Agent SDK {sdk_release} bundles")
    if sdk_release == pinned_sdk and cli_release == pinned_cli:
        raise AdoptionError(f"claude is already at SDK {pinned_sdk} with Claude Code {pinned_cli}")
    if cli_release != pinned_cli:
        path = ROOT / TREES["claude"].pin
        document = _load(path)
        document["cli"] = _claude_cli(cli_release)
        _save(path, document)
    return f"SDK {sdk_release}, Claude Code {cli_release}", []


def adopt(runtime: str, version: str | None, cli_version: str | None = None) -> str:
    """Adopt ``version`` (default: the latest release) and update the documented versions.

    For claude, ``version`` is the Agent SDK's and ``cli_version`` Claude Code's."""
    if runtime == "agy":
        release, conflicts = adopt_agy(version), []
    elif runtime == "claude":
        release, conflicts = adopt_claude(version, cli_version)
    else:
        release, conflicts = adopt_tree(runtime, version)
    _ = sync_docs()
    if conflicts:
        raise AdoptionError(f"{runtime} {release}: TaskSolver's patch conflicts with upstream in "
                            f"{len(conflicts)} file(s) under {TREES[runtime].directory}: " + ", ".join(conflicts))
    return release


def sync_docs(check: bool = False) -> list[str]:
    """Write the pinned versions into the README's table and pycodex's client version.

    Returns the files that were stale; with ``check`` nothing is written."""
    values = versions()
    stale: set[str] = set()
    for name, pattern, template in _DOCS:
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        replacement = template.format(**values)
        updated, count = re.subn(pattern, lambda _match: replacement, text, flags=re.MULTILINE)
        if count != 1:
            raise AdoptionError(f"{name}: expected one match of {pattern!r}, found {count}; "
                                "update _DOCS in tools/runtimes.py")
        if updated != text:
            stale.add(name)
            if not check:
                _ = path.write_text(updated, encoding="utf-8")
    return sorted(stale)


# ---- releases ------------------------------------------------------------------------------------

def _version_lines() -> tuple[list[str], list[int], str]:
    """pyproject.toml's lines, the [project] and [tool.pixi.package] version lines, and the version."""
    lines = (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines(keepends=True)
    section, found, values = "", list[int](), set[str]()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]")
        elif section in ("project", "tool.pixi.package") and stripped.startswith('version = "'):
            found.append(index)
            values.add(stripped.split('"')[1])
    if len(found) != 2 or len(values) != 1:
        raise AdoptionError("pyproject.toml needs one version shared by [project] and [tool.pixi.package]")
    return lines, found, values.pop()


def tasksolver_version() -> str:
    return _version_lines()[2]


def bump() -> str:
    """Raise TaskSolver's patch version in both places pixi and setuptools read it."""
    lines, found, version = _version_lines()
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise AdoptionError(f"cannot bump the non-numeric version {version}")
    new = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    for index in found:
        lines[index] = lines[index].replace(f'"{version}"', f'"{new}"', 1)
    _ = (ROOT / "pyproject.toml").write_text("".join(lines), encoding="utf-8")
    return new


def notes(adopted: Sequence[str]) -> str:
    """Markdown release notes: the runtime versions this release pins."""
    values = versions()
    rows = (("agy", "agy", values["agy"]), ("codex", "Codex", values["codex"]),
            ("claude", "Claude Code (Agent SDK)", f"{values['cli']} ({values['sdk']})"),
            ("kimi", "Kimi Code", values["kimi"]))
    lines = ["## Agent runtimes", "", "| Runtime | Version |", "| --- | --- |"]
    lines += [f"| {label} | {version}{' (updated)' if key in adopted else ''} |" for key, label, version in rows]
    lines += ["", "The attached conda package is a redistributable bundle: it contains Codex and Kimi Code but "
              "not agy or Claude Code, which are built from proprietary executables. After installing it, run "
              "`pixi run reconstruct --target <env>` from a TaskSolver checkout at this tag to rebuild them from "
              "the pinned official releases."]
    return "\n".join(lines) + "\n"


# ---- command line --------------------------------------------------------------------------------

class _Arguments(argparse.Namespace):
    command: str = ""
    runtimes: list[str] = []
    runtime: str = ""
    version: str | None = None
    cli_version: str | None = None
    github_output: str | None = None
    held: str | None = None
    targets: str = ""
    check: bool = False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools/runtimes.py", description=__doc__.split("\n\n")[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="compare the pins with the latest upstream releases")
    _ = check.add_argument("runtimes", nargs="*", metavar="RUNTIME", help=", ".join(RUNTIMES))
    _ = check.add_argument("--github-output", metavar="FILE",
                           help="append `runtimes` and `targets` (outdated ones) for a workflow step")
    _ = check.add_argument("--held", metavar="FILE",
                           help="open issue titles, one per line: hand-work issues hold their runtimes "
                                "at the versions they name")
    title = commands.add_parser("title", help="print the hand-work issue title for a check's targets")
    _ = title.add_argument("targets", help="the check's `targets` output")
    adoption = commands.add_parser("adopt", help="move a runtime to a release (default: the latest)")
    _ = adoption.add_argument("runtime", choices=RUNTIMES)
    _ = adoption.add_argument("--version", help="the release to adopt (claude: the Agent SDK's)")
    _ = adoption.add_argument("--cli-version", help="claude only: the Claude Code release to pin")
    docs = commands.add_parser("docs", help="write the pinned versions into the README and pycodex")
    _ = docs.add_argument("--check", action="store_true", help="only report stale files (exit 1)")
    _ = commands.add_parser("bump", help="raise TaskSolver's patch version and print it")
    _ = commands.add_parser("version", help="print TaskSolver's version")
    release_notes = commands.add_parser("notes", help="print release notes for the pinned runtimes")
    _ = release_notes.add_argument("runtimes", nargs="*", metavar="RUNTIME", help="the runtimes this release updates")
    args = parser.parse_args(argv, namespace=_Arguments())
    unknown = sorted(set(args.runtimes) - set(RUNTIMES))
    if unknown:
        parser.error(f"unknown runtime(s): {', '.join(unknown)} (choose from {', '.join(RUNTIMES)})")
    try:
        if args.command == "check":
            statuses = [Status(name, current(name), latest(name))
                        for runtime in args.runtimes or RUNTIMES for name in COMPONENTS[runtime]]
            for status in statuses:
                state = "update available" if status.outdated else "current"
                print(f"{status.name:<10} pinned {status.current:<10} latest {status.latest:<10} {state}")
            if args.github_output:
                outdated = [status for status in statuses if status.outdated]
                held: set[str] = (held_targets(Path(args.held).read_text(encoding="utf-8").splitlines())
                                  if args.held else set())
                adopt_now = adoptable(outdated, held)
                for status in outdated:
                    if status not in adopt_now:
                        print(f"held: {status.target} (an open issue tracks its hand work)")
                runtimes = " ".join(dict.fromkeys(status.runtime for status in adopt_now))
                with open(args.github_output, "a", encoding="utf-8") as output:
                    _ = output.write(f"runtimes={runtimes}\n")
                    _ = output.write(f"targets={', '.join(status.target for status in adopt_now)}\n")
        elif args.command == "title":
            print(hand_work_title(args.targets))
        elif args.command == "adopt":
            if args.cli_version is not None and args.runtime != "claude":
                parser.error("--cli-version applies to claude only")
            release = adopt(args.runtime, args.version, args.cli_version)
            print(f"adopted {args.runtime} {release}")
        elif args.command == "docs":
            stale = sync_docs(check=args.check)
            if stale:
                print(("stale: " if args.check else "updated: ") + ", ".join(stale))
            return 1 if args.check and stale else 0
        elif args.command == "bump":
            print(bump())
        elif args.command == "version":
            print(tasksolver_version())
        else:
            print(notes(args.runtimes), end="")
    except (AdoptionError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
