#!/usr/bin/env python3
"""Offline checks for tools/runtimes.py, the runtime updater the release workflow runs.

Upstream releases are local git repositories with tags, release manifests are stubbed, and every
repository-root path points at a temporary tree, so nothing is fetched from the network.
Run with ``python test_scripts/test_runtimes.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Final, cast
from unittest.mock import patch

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import runtimes
from runtimes import AdoptionError, Status, Tree

IDENTITY: Final = ["-c", "user.name=test", "-c", "user.email=test@localhost",
                   "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false"]


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(["git", *IDENTITY, *args], cwd=directory, check=True, capture_output=True,
                          text=True).stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text)


LINES: Final = "".join(f"line {number}\n" for number in range(1, 13))


class UpstreamFixture:
    """An upstream repository with tagged releases and a checkout vendoring v1 plus a patch."""

    def __init__(self, root: Path) -> None:
        self.upstream, self.checkout = root / "upstream", root / "checkout"
        self.upstream.mkdir()
        _ = _git(self.upstream, "init", "-q")
        self.release("v1", {"a.txt": LINES, "b.txt": "b1\n", "Cargo.lock": "lock v1\n"})
        self.checkout.mkdir()
        _ = _git(self.checkout, "init", "-q")
        _write(self.checkout / "vendor/up", {
            "a.txt": LINES.replace("line 2\n", "line 2 patched\n"), "b.txt": "b1\n", "w.txt": "ours\n",
            "Cargo.lock": "lock ours\n"})
        _write(self.checkout, {"pin.json": json.dumps({"upstream": {
            "repository": self.upstream.as_uri(), "tag": "v1", "commit": "unknown"}})})
        _ = _git(self.checkout, "add", "-A")
        _ = _git(self.checkout, "commit", "-qm", "vendor v1")
        self.tree = Tree("pin.json", "upstream", "vendor/up", "v", ("Cargo.lock",))

    def release(self, tag: str, files: dict[str, str]) -> None:
        _write(self.upstream, files)
        _ = _git(self.upstream, "add", "-A")
        _ = _git(self.upstream, "commit", "-qm", tag)
        _ = _git(self.upstream, "tag", tag)


class MergeTests(unittest.TestCase):
    def test_patch_replays_onto_a_new_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = UpstreamFixture(Path(directory))
            fixture.release("v2", {"a.txt": LINES.replace("line 11\n", "line 11 upstream\n"), "b.txt": "b2\n",
                                   "Cargo.lock": "lock v2\n"})
            with patch.object(runtimes, "ROOT", fixture.checkout), patch.dict(runtimes.TREES, {"kimi": fixture.tree}):
                release, conflicts = runtimes.adopt_tree("kimi", "2")
            self.assertEqual((release, conflicts), ("2", []))
            vendored = fixture.checkout / "vendor/up"
            self.assertIn("line 2 patched\n", (vendored / "a.txt").read_text())
            self.assertIn("line 11 upstream\n", (vendored / "a.txt").read_text())
            self.assertEqual((vendored / "b.txt").read_text(), "b2\n")
            self.assertEqual((vendored / "w.txt").read_text(), "ours\n")
            self.assertEqual((vendored / "Cargo.lock").read_text(), "lock v2\n")   # the build regenerates it
            pin = cast(dict[str, dict[str, str]], json.loads((fixture.checkout / "pin.json").read_text()))["upstream"]
            self.assertEqual((pin["tag"], pin["commit"]), ("v2", _git(fixture.upstream, "rev-parse", "v2")))
            self.assertEqual(_git(fixture.checkout, "diff", "--cached", "--name-only").split(),
                             ["vendor/up/Cargo.lock", "vendor/up/a.txt", "vendor/up/b.txt"])
            self.assertEqual(_git(fixture.checkout, "diff", "--name-only").split(), ["pin.json"])

    def test_conflicts_are_named_and_left_marked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = UpstreamFixture(Path(directory))
            fixture.release("v2", {"a.txt": LINES.replace("line 2\n", "line 2 upstream\n")})
            with patch.object(runtimes, "ROOT", fixture.checkout), patch.dict(runtimes.TREES, {"kimi": fixture.tree}):
                _, conflicts = runtimes.adopt_tree("kimi", "2")
            self.assertEqual(conflicts, ["a.txt"])
            self.assertIn("<<<<<<<", (fixture.checkout / "vendor/up/a.txt").read_text())

    def test_refuses_uncommitted_changes_and_the_pinned_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = UpstreamFixture(Path(directory))
            fixture.release("v2", {"b.txt": "b2\n"})
            with patch.object(runtimes, "ROOT", fixture.checkout), patch.dict(runtimes.TREES, {"kimi": fixture.tree}):
                with self.assertRaisesRegex(AdoptionError, "already at 1"):
                    _ = runtimes.adopt_tree("kimi", "1")
                _ = (fixture.checkout / "vendor/up/b.txt").write_text("edited\n")
                with self.assertRaisesRegex(AdoptionError, "uncommitted changes"):
                    _ = runtimes.adopt_tree("kimi", "2")


class AgyAndClaudeTests(unittest.TestCase):
    def test_agy_pins_the_archive_and_the_extracted_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "antigravity").mkdir()
            binary = b"agy 9.9.9 executable"
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w:gz") as bundle:
                member = tarfile.TarInfo("antigravity")
                member.size, member.mode = len(binary), 0o755
                bundle.addfile(member, io.BytesIO(binary))
            archive = payload.getvalue()
            manifest: dict[str, object] = {"version": "9.9.9", "url": "https://example.invalid/agy.tar.gz",
                                           "sha512": hashlib.sha512(archive).hexdigest()}

            def download(_url: str, target: Path) -> None:
                _ = target.write_bytes(archive)

            with patch.object(runtimes, "ROOT", root), patch.object(runtimes, "_get", return_value=manifest), \
                    patch.object(runtimes, "_download", side_effect=download):
                self.assertEqual(runtimes.adopt_agy(None), "9.9.9")
                with self.assertRaisesRegex(AdoptionError, "serves only 9.9.9"):
                    _ = runtimes.adopt_agy("1.0.0")
            pin = cast(dict[str, dict[str, object]], json.loads((root / "antigravity/vendor.json").read_text()))["cli"]
            self.assertEqual(pin, {"version": "9.9.9", "url": manifest["url"], "archive_sha512": manifest["sha512"],
                                   "sha512": hashlib.sha512(binary).hexdigest(), "size": len(binary)})
            cache = root / "antigravity/build" / f"antigravity-{manifest['sha512']}.raw"
            self.assertEqual(cache.read_bytes(), binary)

    def test_claude_cli_pin_comes_from_the_release_manifest(self) -> None:
        manifest: dict[str, object] = {"version": "9.9.9", "platforms": {
            "linux-x64": {"binary": "claude", "checksum": "ab" * 32, "size": 123}}}
        with patch.object(runtimes, "_get", return_value=manifest) as get:
            pin = runtimes._claude_cli("9.9.9")
        get.assert_called_once_with(f"{runtimes.CLAUDE_RELEASES}/9.9.9/manifest.json")
        self.assertEqual(pin, {"version": "9.9.9", "url": f"{runtimes.CLAUDE_RELEASES}/9.9.9/linux-x64/claude",
                               "sha256": "ab" * 32, "size": 123})

    def test_claude_code_is_adopted_on_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = {"version": "2.0.0", "url": "https://example.invalid/2.0.0", "sha256": "00" * 32, "size": 1}
            _write(root, {
                "claude/vendor.json": json.dumps({"sdk": {"repository": "https://example.invalid/sdk.git",
                                                          "tag": "v1.0.0", "commit": "c0ffee"}, "cli": cli}),
                "claude/vendor/sdk/src/claude_agent_sdk/_cli_version.py": '__cli_version__ = "2.0.0"\n'})
            releases = {"claude-sdk": "1.0.0", "claude-cli": "2.0.1"}

            def newest(component: str) -> str:
                return releases[component]

            manifest: dict[str, object] = {"platforms": {"linux-x64": {"checksum": "cd" * 32, "size": 2}}}
            with patch.object(runtimes, "ROOT", root), patch.object(runtimes, "latest", side_effect=newest), \
                    patch.object(runtimes, "_get", return_value=manifest):
                self.assertEqual(runtimes.adopt_claude(None, None), ("SDK 1.0.0, Claude Code 2.0.1", []))
                with self.assertRaisesRegex(AdoptionError, "already at SDK 1.0.0 with Claude Code 2.0.1"):
                    _ = runtimes.adopt_claude(None, None)
                with self.assertRaisesRegex(AdoptionError, "older than the 2.0.0 Agent SDK 1.0.0 bundles"):
                    _ = runtimes.adopt_claude(None, "1.9.9")
            pin = cast(dict[str, dict[str, object]], json.loads((root / "claude/vendor.json").read_text()))
            self.assertEqual(pin["sdk"]["tag"], "v1.0.0")
            self.assertEqual(pin["cli"], {"version": "2.0.1", "url": f"{runtimes.CLAUDE_RELEASES}/2.0.1/linux-x64/claude",
                                          "sha256": "cd" * 32, "size": 2})


class ReleaseTests(unittest.TestCase):
    def test_version_order(self) -> None:
        self.assertTrue(Status("agy", "1.4.11", "1.4.12").outdated)
        self.assertEqual((Status("claude-cli", "2.1.316", "2.1.317").runtime, Status("claude-sdk", "1", "1").runtime),
                         ("claude", "claude"))
        self.assertTrue(Status("kimi", "2.9.9", "2.10.0").outdated)
        self.assertFalse(Status("codex", "0.166.0", "0.166.0").outdated)
        self.assertFalse(Status("codex", "0.167.0", "0.166.0").outdated)

    def test_documented_versions_match_the_pins(self) -> None:
        self.assertEqual(runtimes.sync_docs(check=True), [])

    def test_docs_follow_the_pins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("README.md", "codex/pycodex/sdk.py", "antigravity/vendor.json", "codex/vendor.json",
                         "claude/vendor.json", "kimi/vendor.json"):
                _write(root, {name: (ROOT / name).read_text()})
            pin = cast(dict[str, dict[str, str]], json.loads((root / "codex/vendor.json").read_text()))
            pin["upstream"]["tag"] = "rust-v9.9.9"
            _write(root, {"codex/vendor.json": json.dumps(pin)})
            with patch.object(runtimes, "ROOT", root):
                self.assertEqual(runtimes.sync_docs(check=True), ["README.md", "codex/pycodex/sdk.py"])
                self.assertEqual(runtimes.sync_docs(), ["README.md", "codex/pycodex/sdk.py"])
                self.assertEqual(runtimes.sync_docs(check=True), [])
            self.assertIn("| Codex | **9.9.9** (`rust-v9.9.9`) |", (root / "README.md").read_text())
            self.assertIn('client_version="9.9.9"', (root / "codex/pycodex/sdk.py").read_text())

    def test_bump_raises_both_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, {"pyproject.toml": '[project]\nname = "x"\nversion = "0.0.9"\n\n'
                                            '[tool.pixi.package]\nname = "x"\nversion = "0.0.9"\n'})
            with patch.object(runtimes, "ROOT", root):
                self.assertEqual(runtimes.bump(), "0.0.10")
                self.assertEqual(runtimes.tasksolver_version(), "0.0.10")
            self.assertEqual((root / "pyproject.toml").read_text().count('version = "0.0.10"'), 2)
            _write(root, {"pyproject.toml": '[project]\nversion = "0.0.1"\n[tool.pixi.package]\nversion = "0.0.2"\n'})
            with patch.object(runtimes, "ROOT", root), self.assertRaisesRegex(AdoptionError, "one version"):
                _ = runtimes.bump()

    def test_check_reports_outdated_runtimes_for_the_workflow(self) -> None:
        latest = {"agy": "9.9.9", "codex": runtimes.current("codex"), "claude-sdk": runtimes.current("claude-sdk"),
                  "claude-cli": runtimes.current("claude-cli"), "kimi": "9.9.9"}

        def release(runtime: str) -> str:
            return latest[runtime]

        with tempfile.TemporaryDirectory() as directory, patch.object(runtimes, "latest", side_effect=release):
            output = Path(directory) / "output"
            self.assertEqual(runtimes.main(["check", "--github-output", str(output)]), 0)
            self.assertEqual(output.read_text(), "runtimes=agy kimi\ntargets=agy 9.9.9, kimi 9.9.9\n")
            latest |= {"agy": runtimes.current("agy"), "kimi": runtimes.current("kimi"), "claude-cli": "9.9.9"}
            output.unlink()
            self.assertEqual(runtimes.main(["check", "claude", "--github-output", str(output)]), 0)
            self.assertEqual(output.read_text(), "runtimes=claude\ntargets=claude-cli 9.9.9\n")
        self.assertIn("| agy | ", runtimes.notes(["agy"]))
        self.assertIn("(updated)", runtimes.notes(["agy"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
