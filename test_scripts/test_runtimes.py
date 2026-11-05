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

    def test_hand_work_issues_hold_only_their_runtime_at_their_versions(self) -> None:
        latest = {"agy": "9.9.9", "codex": runtimes.current("codex"), "claude-sdk": "9.9.9",
                  "claude-cli": "9.9.9", "kimi": "9.9.9"}

        def release(runtime: str) -> str:
            return latest[runtime]

        with tempfile.TemporaryDirectory() as directory, patch.object(runtimes, "latest", side_effect=release):
            output, issues = Path(directory) / "output", Path(directory) / "issues"
            # claude is held at exactly these versions; kimi's issue names an older release.
            _ = issues.write_text(runtimes.hand_work_title("claude-sdk 9.9.9, claude-cli 9.9.9") + "\n"
                                  + runtimes.hand_work_title("kimi 1.0.0") + "\nAn unrelated issue\n")
            self.assertEqual(runtimes.main(["check", "--github-output", str(output), "--held", str(issues)]), 0)
            self.assertEqual(output.read_text(), "runtimes=agy kimi\ntargets=agy 9.9.9, kimi 9.9.9\n")
            latest["claude-cli"] = "9.9.10"  # a newer Claude Code release retries the held runtime
            output.unlink()
            self.assertEqual(runtimes.main(["check", "--github-output", str(output), "--held", str(issues)]), 0)
            self.assertEqual(output.read_text(), "runtimes=agy claude kimi\n"
                             "targets=agy 9.9.9, claude-sdk 9.9.9, claude-cli 9.9.10, kimi 9.9.9\n")
        with patch("sys.stdout", new_callable=io.StringIO) as printed:
            self.assertEqual(runtimes.main(["title", "agy 9.9.9"]), 0)
        self.assertEqual(printed.getvalue(), "Runtime update needs hand work: agy 9.9.9\n")


HOOKS: Final = (
    runtimes.Hook(r"import \{ createHash \} from 'node:crypto';", "import { emitEvent, emitRequest } from '#/wiretap';"),
    runtimes.Hook(r"(?P<indent>\s*)(?:const )?recorded = this\.recordRequest\((?P<input>\w+)\);",
                  "{indent}emitRequest({input});"),
    runtimes.Hook(r"(?P<indent>\s*)switch \(event\.type\) \{", "{indent}emitEvent(event);", before=True),
)
OLD_SOURCE: Final = (
    "import { createHash } from 'node:crypto';\n"
    "import { Service } from '#/service';\n"
    "export class Requester extends Service {\n"
    "  async run(request: Request): Promise<Finish> {\n"
    "    const logInput = this.describe(request);\n"
    "    this.logRequest(logInput);\n"
    "    const recorded = this.recordRequest(logInput);\n"
    "    let finish: Finish | undefined;\n"
    "    for await (const event of request.requester.request(input, signal)) {\n"
    "      switch (event.type) {\n"
    "        case 'part':\n"
    "          await onPart(event.part);\n"
    "          break;\n"
    "        case 'finish':\n"
    "          finish = event;\n"
    "          break;\n"
    "      }\n"
    "    }\n"
    "    return finish;\n"
    "  }\n"
    "}\n"
)
# The next release renames the file, logs the request from a callback and adds an event.
NEW_SOURCE: Final = OLD_SOURCE.replace(
    "    const recorded = this.recordRequest(logInput);\n"
    "    let finish: Finish | undefined;\n"
    "    for await (const event of request.requester.request(input, signal)) {\n",
    "    let recorded: Recorded;\n"
    "    let finish: Finish | undefined;\n"
    "    for await (const event of generateModel(request, signal, {\n"
    "      onAttempt: (attemptInput) => {\n"
    "        recorded = this.recordRequest(attemptInput);\n"
    "      },\n"
    "    })) {\n",
).replace("        case 'finish':\n", "        case 'usage':\n          usage = event.usage;\n          break;\n        case 'finish':\n")


def patched(source: str) -> str:
    text = runtimes.apply_hooks(HOOKS, source)
    assert text is not None
    return text


class HookFixture(UpstreamFixture):
    """v1 vendors src/old.ts with the hooks; v2 renames it to src/new.ts and rewrites it."""

    def __init__(self, root: Path, new_source: str = NEW_SOURCE) -> None:
        super().__init__(root)
        self.release("v1.1", {"src/old.ts": OLD_SOURCE})
        _write(self.checkout / "vendor/up", {"src/old.ts": patched(OLD_SOURCE)})
        _ = (self.checkout / "pin.json").write_text(json.dumps({"upstream": {
            "repository": self.upstream.as_uri(), "tag": "v1.1", "commit": "unknown"}}))
        _ = _git(self.checkout, "add", "-A")
        _ = _git(self.checkout, "commit", "-qm", "vendor v1.1 with hooks")
        (self.upstream / "src/old.ts").unlink()
        self.release("v2", {"src/new.ts": new_source})
        self.tree = Tree("pin.json", "upstream", "vendor/up", "v", ("Cargo.lock",), (HOOKS,))


class HookTests(unittest.TestCase):
    def test_hooks_are_placed_by_their_anchors(self) -> None:
        text = patched(NEW_SOURCE)
        lines = text.splitlines()
        self.assertEqual(lines[1], "import { emitEvent, emitRequest } from '#/wiretap';")
        self.assertEqual(lines[lines.index("        recorded = this.recordRequest(attemptInput);") + 1],
                         "        emitRequest(attemptInput);")
        self.assertEqual(lines[lines.index("      switch (event.type) {") - 1], "      emitEvent(event);")
        self.assertEqual(text.replace("import { emitEvent, emitRequest } from '#/wiretap';\n", "")
                         .replace("        emitRequest(attemptInput);\n", "").replace("      emitEvent(event);\n", ""),
                         NEW_SOURCE)
        self.assertIsNone(runtimes.apply_hooks(HOOKS, text), "already hooked")
        self.assertIsNone(runtimes.apply_hooks(HOOKS, NEW_SOURCE.replace("      switch (event.type) {\n",
                                                                         "      switch (event.type) {\n" * 2)),
                          "an ambiguous anchor")
        self.assertIsNone(runtimes.apply_hooks(HOOKS, NEW_SOURCE.replace("createHash", "createHmac")), "a missing anchor")

    def test_hooks_follow_a_renamed_and_rewritten_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HookFixture(Path(directory))
            with patch.object(runtimes, "ROOT", fixture.checkout), patch.dict(runtimes.TREES, {"kimi": fixture.tree}), \
                    patch("sys.stdout", new_callable=io.StringIO) as printed:
                release, conflicts = runtimes.adopt_tree("kimi", "2")
            self.assertEqual((release, conflicts), ("2", []))
            self.assertIn("re-inserted", printed.getvalue())
            self.assertEqual((fixture.checkout / "vendor/up/src/new.ts").read_text(), patched(NEW_SOURCE))
            self.assertFalse((fixture.checkout / "vendor/up/src/old.ts").exists())
            self.assertEqual(_git(fixture.checkout, "status", "--porcelain", "--", "vendor/up/src/new.ts"), "A  vendor/up/src/new.ts")

    def test_an_ambiguous_anchor_leaves_the_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            twice = NEW_SOURCE.replace("      switch (event.type) {\n", "      switch (event.type) {\n      switch (event.type) {\n")
            fixture = HookFixture(Path(directory), twice)
            with patch.object(runtimes, "ROOT", fixture.checkout), patch.dict(runtimes.TREES, {"kimi": fixture.tree}):
                _, conflicts = runtimes.adopt_tree("kimi", "2")
            self.assertEqual(conflicts, ["src/new.ts"])
            self.assertIn("<<<<<<<", (fixture.checkout / "vendor/up/src/new.ts").read_text())


class ShelveTests(unittest.TestCase):
    def test_shelve_commits_the_attempt_and_restores_the_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = UpstreamFixture(Path(directory))
            fixture.release("v2", {"a.txt": LINES.replace("line 2\n", "line 2 upstream\n"), "n.txt": "new\n"})
            head = _git(fixture.checkout, "rev-parse", "HEAD")
            patches = (patch.object(runtimes, "ROOT", fixture.checkout), patch.dict(runtimes.TREES, {"kimi": fixture.tree}),
                       patch.dict(runtimes.RUNTIME_PATHS, {"kimi": ("pin.json", "vendor/up")}),
                       patch.object(runtimes, "sync_docs", return_value=[]))
            for active in patches:
                _ = active.start()
                self.addCleanup(active.stop)
            _, conflicts = runtimes.adopt_tree("kimi", "2")
            self.assertEqual(conflicts, ["a.txt"])
            commit = runtimes.shelve("kimi", "Adopt kimi 2 (needs hand work)")
            assert commit is not None
            self.assertEqual(_git(fixture.checkout, "rev-parse", "HEAD"), head)
            self.assertEqual(_git(fixture.checkout, "rev-parse", f"{commit}^"), head)
            self.assertEqual(_git(fixture.checkout, "log", "-1", "--format=%s", commit), "Adopt kimi 2 (needs hand work)")
            self.assertIn("<<<<<<<", _git(fixture.checkout, "show", f"{commit}:vendor/up/a.txt"))
            self.assertEqual(_git(fixture.checkout, "show", f"{commit}:vendor/up/n.txt"), "new")
            self.assertIn('"tag": "v2"', _git(fixture.checkout, "show", f"{commit}:pin.json"))
            # The checkout is back at HEAD's runtime, clean, with the release's new file gone.
            self.assertEqual(_git(fixture.checkout, "status", "--porcelain"), "")
            self.assertEqual((fixture.checkout / "vendor/up/a.txt").read_text(), LINES.replace("line 2\n", "line 2 patched\n"))
            self.assertFalse((fixture.checkout / "vendor/up/n.txt").exists())
            self.assertIn('"tag": "v1"', (fixture.checkout / "pin.json").read_text())
            _, conflicts = runtimes.adopt_tree("kimi", "2")
            self.assertEqual(conflicts, ["a.txt"])
            self.assertIsNone(runtimes.shelve("kimi", None))
            self.assertEqual(_git(fixture.checkout, "status", "--porcelain"), "")


class WorkflowHelperTests(unittest.TestCase):
    def test_targets_are_filtered_by_runtime(self) -> None:
        targets = "agy 1.5.1, claude-sdk 0.2.178, claude-cli 2.1.318, kimi 2.5.1"
        self.assertEqual(runtimes.targets_for(targets, ["claude", "kimi"]), "claude-sdk 0.2.178, claude-cli 2.1.318, kimi 2.5.1")
        self.assertEqual(runtimes.targets_for(targets, []), "")
        with patch("sys.stdout", new_callable=io.StringIO) as printed:
            self.assertEqual(runtimes.main(["targets", targets, "agy"]), 0)
        self.assertEqual(printed.getvalue(), "agy 1.5.1\n")

    def test_temporary_failures_exit_75(self) -> None:
        import email.message
        import urllib.error
        unavailable = urllib.error.HTTPError("https://example.invalid", 503, "unavailable", email.message.Message(), None)
        missing = urllib.error.HTTPError("https://example.invalid", 404, "missing", email.message.Message(), None)
        self.assertTrue(runtimes.temporary(urllib.error.URLError("name resolution failed")))
        self.assertTrue(runtimes.temporary(unavailable))
        self.assertTrue(runtimes.temporary(TimeoutError()))
        self.assertTrue(runtimes.temporary(AdoptionError("git fetch failed: fatal: unable to access 'https://x': Could not resolve host: x")))
        self.assertFalse(runtimes.temporary(missing))
        self.assertFalse(runtimes.temporary(AdoptionError("kimi 2: TaskSolver's patch conflicts with upstream")))
        with patch.object(runtimes, "latest", side_effect=urllib.error.URLError("down")), \
                patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertEqual(runtimes.main(["check", "agy"]), runtimes.EX_TEMPFAIL)
        self.assertIn("temporary failure", errors.getvalue())
        with patch.object(runtimes, "latest", side_effect=missing), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(runtimes.main(["check", "agy"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
