#!/usr/bin/env python3
"""Offline checks for Claude's pinned download and wheel package contents.

The downloader uses synthetic bytes; the package check uses an already-built
runtime and runs only ``--version`` outside the checkout. No model or network.
Run with ``python test_scripts/test_claude_bundle.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, TypedDict, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from claude.build_cli import CLIPin
    from setup import BuildPyNative

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BUILD_SCRIPT: Final = ROOT / "claude/build_cli.py"
SDK_SOURCE: Final = ROOT / "claude/vendor/sdk/src"


from wirecap.types import (
    JSONValue,
    json_array,
    json_loads,
    json_loads_object,
    json_object,
)


class BuildFunction(Protocol):
    __globals__: dict[str, object]

    def __call__(self) -> Path: ...


class DownloadModule(TypedDict):
    build: BuildFunction
    fetch_cli: Callable[[Path, CLIPin], Path]
    matches: Callable[[Path, CLIPin], bool]
    _load_pin: Callable[[Path], CLIPin]


class ClaudeDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tasksolver-claude-download-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "vendor/sdk/src/claude_agent_sdk"
        self.package.mkdir(parents=True)
        (self.package / "_cli_version.py").write_text('__cli_version__ = "test-version"\n')
        self.payload = b"verified executable payload\n"
        self.pin: CLIPin = {"version": "test-version", "url": "https://example.invalid/claude",
                    "size": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest()}
        (self.root / "vendor.json").write_text(json.dumps({"cli": self.pin}))
        self.module = cast(DownloadModule, runpy.run_path(str(BUILD_SCRIPT)))
        self.build = self.module["build"]
        self.matches = self.module["matches"]
        self.target = self.root / "build/cache" / f"claude-{self.pin['sha256']}"
        self.addCleanup(patch.stopall)
        old_file = self.build.__globals__["__file__"]
        self.addCleanup(self.build.__globals__.__setitem__, "__file__", old_file)
        self.build.__globals__["__file__"] = str(self.root / "build_cli.py")
        patch("platform.system", return_value="Linux").start()
        patch("platform.machine", return_value="x86_64").start()

    def fetch(self) -> Path:
        return self.module["fetch_cli"](self.root, self.pin)

    def test_verified_download_is_atomic_private_and_cached(self) -> None:
        with patch("urllib.request.urlopen", return_value=io.BytesIO(self.payload)) as download:
            self.assertEqual(self.fetch(), self.target)
        download.assert_called_once_with(self.pin["url"], timeout=60)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.target.parent.glob(".claude-*")), [])
        self.target.chmod(0o600)
        with patch("urllib.request.urlopen", side_effect=AssertionError("Cache hit downloaded")):
            self.assertEqual(self.fetch(), self.target)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_same_length_checksum_mismatch_preserves_previous_target(self) -> None:
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"previous artifact")
        corrupt: Final = b"x" * len(self.payload)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(corrupt)), \
                self.assertRaisesRegex(RuntimeError, "verification"):
            self.fetch()
        self.assertEqual(self.target.read_bytes(), b"previous artifact")
        self.assertEqual(list(self.target.parent.glob(".claude-*")), [])

    def test_truncated_download_is_rejected_and_removed(self) -> None:
        with patch("urllib.request.urlopen", return_value=io.BytesIO(self.payload[:-1])), \
                self.assertRaisesRegex(RuntimeError, "verification"):
            self.fetch()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".claude-*")), [])

    def test_network_failure_removes_temporary_file(self) -> None:
        with patch("urllib.request.urlopen", side_effect=OSError("offline")), \
                self.assertRaisesRegex(OSError, "offline"):
            self.fetch()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".claude-*")), [])

    def test_sdk_cli_version_disagreement_rejected_before_download(self) -> None:
        (self.package / "_cli_version.py").write_text('__cli_version__ = "different"\n')
        with patch("urllib.request.urlopen") as download, \
                self.assertRaisesRegex(RuntimeError, "does not satisfy"):
            self.build()
        download.assert_not_called()

    def test_cli_pin_may_run_ahead_of_the_sdk(self) -> None:
        pin: CLIPin = {**self.pin, "version": "2.1.317"}
        (self.root / "vendor.json").write_text(json.dumps({"cli": pin}))
        for bundled, accepted in (("2.1.316", True), ("2.1.317", True), ("2.1.318", False)):
            with self.subTest(bundled=bundled):
                (self.package / "_cli_version.py").write_text(f'__cli_version__ = "{bundled}"\n')
                # Past the version rule the build reaches its platform check.
                expected = "Linux x86-64" if accepted else "does not satisfy"
                with patch("platform.machine", return_value="aarch64"), \
                        patch("urllib.request.urlopen") as download, \
                        self.assertRaisesRegex(RuntimeError, expected):
                    self.build()
                download.assert_not_called()

    def test_wrong_platform_rejected_before_download(self) -> None:
        with patch("platform.machine", return_value="aarch64"), \
                patch("urllib.request.urlopen") as download, \
                self.assertRaisesRegex(RuntimeError, "Linux x86-64"):
            self.build()
        download.assert_not_called()


class ClaudePackageTests(unittest.TestCase):
    def test_builder_imports_without_installed_packages(self) -> None:
        probe = subprocess.run(
            [sys.executable, "-I", "-S", "-c",
             "import runpy, sys; runpy.run_path(sys.argv[1]); "
             "assert 'wirecap' not in sys.modules; print('standalone')",
             str(BUILD_SCRIPT)],
            cwd="/tmp", text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "standalone")

    def test_sdk_discovery_license_and_runtime_dependencies(self) -> None:
        config = json_object(cast(JSONValue, tomllib.loads((ROOT / "pyproject.toml").read_text())))
        upstream = json_object(cast(JSONValue, tomllib.loads((SDK_SOURCE.parent / "pyproject.toml").read_text())))
        pins: Final = json_loads_object((ROOT / "claude/vendor.json").read_text())
        project = json_object(config["project"])
        upstream_project = json_object(upstream["project"])
        upstream_version = upstream_project["version"]
        assert isinstance(upstream_version, str)
        self.assertEqual(json_object(pins["sdk"])["tag"], "v" + upstream_version)
        self.assertEqual(cast(dict[str, object], runpy.run_path(str(SDK_SOURCE / "claude_agent_sdk/_version.py")))[
            "__version__"], upstream_version)
        setuptools = json_object(json_object(config["tool"])["setuptools"])
        find = json_object(json_object(setuptools["packages"])["find"])
        self.assertIn("claude/vendor/sdk/src", json_array(find["where"]))
        self.assertIn("claude_agent_sdk*", json_array(find["include"]))
        self.assertIn("claude/vendor/sdk/LICENSE", json_array(setuptools["license-files"]))
        self.assertIn("_bundled/claude", json_array(json_object(setuptools["package-data"])["claude_agent_sdk"]))
        self.assertTrue((SDK_SOURCE.parent / "LICENSE").is_file())
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name

        def dependency_name(spec: JSONValue) -> str:
            assert isinstance(spec, str)
            return canonicalize_name(Requirement(spec).name)

        declared = {dependency_name(spec) for spec in json_array(project["dependencies"])}
        for dependency in json_array(upstream_project["dependencies"]):
            self.assertIn(dependency_name(dependency), declared)
        self.assertNotIn("claude-agent-sdk", declared)

    def test_native_package_retains_relocatable_bridged_cli(self) -> None:
        helpers = cast(DownloadModule, runpy.run_path(str(BUILD_SCRIPT)))
        pin = helpers["_load_pin"](ROOT / "claude/vendor.json")
        source: Final = SDK_SOURCE / "claude_agent_sdk/_bundled/claude"
        if not source.exists():
            self.skipTest("Run pixi install to build the pinned Claude runtime first")
        from claude.build_cli import _bundle_matches
        source_manifest = json_loads_object(source.with_name("manifest.json").read_text())
        fingerprint = source_manifest["build_fingerprint"]
        assert isinstance(fingerprint, str)
        self.assertTrue(_bundle_matches(source.parent, fingerprint))
        self.assertEqual(source_manifest["original_sha256"], pin["sha256"])
        self.assertIs(source_manifest["native_bridge"], True)
        self.assertFalse(helpers["matches"](source, pin), "Default CLI must be the split-runtime launcher")

        from setuptools import Distribution, find_packages

        with patch("setuptools.setup"):
            setup = cast(dict[str, object], runpy.run_path(str(ROOT / "setup.py")))
        config = json_object(cast(JSONValue, tomllib.loads((ROOT / "pyproject.toml").read_text())))
        package_data = json_object(json_object(json_object(config["tool"])["setuptools"])["package-data"])
        sdk_patterns: list[str] = []
        for item in json_array(package_data["claude_agent_sdk"]):
            assert isinstance(item, str)
            sdk_patterns.append(item)
        with tempfile.TemporaryDirectory(prefix="tasksolver-claude-package-") as directory:
            sandbox: Final = Path(directory)
            stage: Final = sandbox / "wheel"
            distribution: Final = Distribution({
                "name": "tasksolver-claude-offline-package-test",
                "script_name": str(ROOT / "setup.py"),
                "packages": ["pyclaude", *find_packages(str(SDK_SOURCE))],
                "package_dir": {"pyclaude": str(ROOT / "claude/pyclaude"),
                                "claude_agent_sdk": str(SDK_SOURCE / "claude_agent_sdk")},
                "package_data": {"claude_agent_sdk": sdk_patterns},
            })
            builder_type = cast("type[BuildPyNative]", setup["BuildPyNative"])
            builder: Final = builder_type(distribution)
            builder.ensure_finalized()
            builder.build_lib = str(stage)
            builder.compile = False
            builder.force = True
            with patch.object(builder, "_build_bridge"), \
                    patch.object(builder, "_build_antigravity_shim"), \
                    patch.object(builder, "_build_codex"), \
                    patch.object(builder, "_build_kimi"), \
                    patch.object(builder, "_bundle_artifacts"), \
                    patch("subprocess.run") as native_build:
                builder.run()
            native_build.assert_called_once_with(
                [sys.executable, "-m", "claude.build_cli"], cwd=str(ROOT),
                env=cast(Callable[[], dict[str, str]], setup["native_build_env"])(), check=True)
            bundled: Final = stage / "claude_agent_sdk/_bundled/claude"
            self.assertTrue(_bundle_matches(bundled.parent, fingerprint))
            self.assertTrue(os.access(bundled, os.X_OK))
            self.assertTrue(os.access(bundled.with_name("bun"), os.X_OK))
            self.assertTrue((stage / "pyclaude/client.py").is_file())
            self.assertTrue((stage / "claude_agent_sdk/py.typed").is_file())
            env: Final = dict(os.environ, PYTHONPATH=str(stage), DISABLE_AUTOUPDATER="1")
            probe: Final = subprocess.run(
                [sys.executable, "-c",
                 "import json; from pathlib import Path; import claude_agent_sdk; "
                 "from claude_agent_sdk import _cli_version; "
                 "print(json.dumps([str(Path(claude_agent_sdk.__file__).parent), "
                 "_cli_version.__cli_version__]))"],
                cwd=sandbox, env=env, text=True, capture_output=True, check=True, timeout=30,
            )
            # The SDK keeps naming the release it bundles upstream; the pin may run ahead of it.
            sdk_cli_version: Final = cast(dict[str, object], runpy.run_path(
                str(SDK_SOURCE / "claude_agent_sdk/_cli_version.py")))["__cli_version__"]
            self.assertEqual(json_loads(probe.stdout),
                             [str(stage / "claude_agent_sdk"), sdk_cli_version])
            version: Final = subprocess.run([str(bundled), "--version"], cwd=sandbox,
                                            env=env, text=True, capture_output=True,
                                            check=True, timeout=30)
            self.assertIn(pin["version"], version.stdout)
            manifest_path = bundled.with_name("manifest.json")
            manifest = json_loads_object(manifest_path.read_text())
            corruptions: list[tuple[str, JSONValue]] = [
                ("code_sha256", {}), ("bun_sha256", "0" * 64), ("native_bridge", False)]
            for field in ("cache_sha256", "asset_sha256", "native_module_sha256", "helper_sha256"):
                omitted = dict(json_object(manifest[field]))
                self.assertTrue(omitted, field)
                omitted.pop("libwire.node" if field == "helper_sha256" else next(iter(omitted)))
                corruptions.append((field, omitted))
            for field, value in corruptions:
                with self.subTest(field=field):
                    corrupted = dict(manifest)
                    corrupted[field] = value
                    manifest_path.write_text(json.dumps(corrupted))
                    self.assertFalse(_bundle_matches(bundled.parent, fingerprint))
            manifest_path.write_text(json.dumps(manifest))


if __name__ == "__main__":
    unittest.main(verbosity=2)
