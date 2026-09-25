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
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import tomllib
from typing import Final
import unittest
from unittest.mock import patch

ROOT: Final = Path(__file__).resolve().parents[1]
BUILD_SCRIPT: Final = ROOT / "claude/build_cli.py"
SDK_SOURCE: Final = ROOT / "claude/vendor/sdk/src"


class ClaudeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tasksolver-claude-download-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "vendor/sdk/src/claude_agent_sdk"
        self.package.mkdir(parents=True)
        (self.package / "_cli_version.py").write_text('__cli_version__ = "test-version"\n')
        self.payload = b"verified executable payload\n"
        self.pin = {"version": "test-version", "url": "https://example.invalid/claude",
                    "size": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest()}
        (self.root / "vendor.json").write_text(json.dumps({"cli": self.pin}))
        self.module = runpy.run_path(str(BUILD_SCRIPT))
        self.build = self.module["build"]
        self.matches = self.module["matches"]
        self.target = self.package / "_bundled/claude"
        self.addCleanup(patch.stopall)
        patch.dict(self.build.__globals__, {"__file__": str(self.root / "build_cli.py")}).start()
        patch("platform.system", return_value="Linux").start()
        patch("platform.machine", return_value="x86_64").start()

    def test_verified_download_is_atomic_executable_and_cached(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(self.payload)) as download:
            self.assertEqual(self.build(), self.target)
        download.assert_called_once_with(self.pin["url"], timeout=60)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(self.target.parent.glob(".claude-*")), [])
        self.target.chmod(0o600)
        with patch("urllib.request.urlopen", side_effect=AssertionError("Cache hit downloaded")):
            self.assertEqual(self.build(), self.target)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)

    def test_same_length_checksum_mismatch_preserves_previous_target(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b"previous artifact")
        corrupt: Final = b"x" * len(self.payload)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(corrupt)), \
                self.assertRaisesRegex(RuntimeError, "verification"):
            self.build()
        self.assertEqual(self.target.read_bytes(), b"previous artifact")
        self.assertEqual(list(self.target.parent.glob(".claude-*")), [])

    def test_truncated_download_is_rejected_and_removed(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(self.payload[:-1])), \
                self.assertRaisesRegex(RuntimeError, "verification"):
            self.build()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".claude-*")), [])

    def test_network_failure_removes_temporary_file(self):
        with patch("urllib.request.urlopen", side_effect=OSError("offline")), \
                self.assertRaisesRegex(OSError, "offline"):
            self.build()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".claude-*")), [])

    def test_sdk_cli_version_disagreement_rejected_before_download(self):
        (self.package / "_cli_version.py").write_text('__cli_version__ = "different"\n')
        with patch("urllib.request.urlopen") as download, \
                self.assertRaisesRegex(RuntimeError, "pin does not match"):
            self.build()
        download.assert_not_called()

    def test_wrong_platform_rejected_before_download(self):
        with patch("platform.machine", return_value="aarch64"), \
                patch("urllib.request.urlopen") as download, \
                self.assertRaisesRegex(RuntimeError, "Linux x86-64"):
            self.build()
        download.assert_not_called()


class ClaudePackageTests(unittest.TestCase):
    def test_sdk_discovery_license_and_runtime_dependencies(self):
        config: Final = tomllib.loads((ROOT / "pyproject.toml").read_text())
        upstream: Final = tomllib.loads((SDK_SOURCE.parent / "pyproject.toml").read_text())
        pins: Final = json.loads((ROOT / "claude/vendor.json").read_text())
        self.assertEqual(pins["sdk"]["tag"], "v" + upstream["project"]["version"])
        self.assertEqual(runpy.run_path(str(SDK_SOURCE / "claude_agent_sdk/_version.py"))[
            "__version__"], upstream["project"]["version"])
        setuptools: Final = config["tool"]["setuptools"]
        self.assertIn("claude/vendor/sdk/src", setuptools["packages"]["find"]["where"])
        self.assertIn("claude_agent_sdk*", setuptools["packages"]["find"]["include"])
        self.assertIn("claude/vendor/sdk/LICENSE", setuptools["license-files"])
        self.assertIn("_bundled/claude", setuptools["package-data"]["claude_agent_sdk"])
        self.assertTrue((SDK_SOURCE.parent / "LICENSE").is_file())
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name

        declared: Final = {
            canonicalize_name(Requirement(spec).name) for spec in config["project"]["dependencies"]
        }
        for dependency in upstream["project"]["dependencies"]:
            self.assertIn(canonicalize_name(Requirement(dependency).name), declared)
        self.assertNotIn("claude-agent-sdk", declared)

    def test_native_package_retains_verified_bundled_cli(self):
        pin: Final = json.loads((ROOT / "claude/vendor.json").read_text())["cli"]
        source: Final = SDK_SOURCE / "claude_agent_sdk/_bundled/claude"
        if not source.exists():
            self.skipTest("Run pixi install to build the pinned Claude runtime first")
        helpers: Final = runpy.run_path(str(BUILD_SCRIPT))
        self.assertTrue(helpers["matches"](source, pin), "Built CLI must match its pin")

        from setuptools import Distribution, find_packages

        with patch("setuptools.setup"):
            setup: Final = runpy.run_path(str(ROOT / "setup.py"))
        with tempfile.TemporaryDirectory(prefix="tasksolver-claude-package-") as directory:
            sandbox: Final = Path(directory)
            stage: Final = sandbox / "wheel"
            distribution: Final = Distribution({
                "name": "tasksolver-claude-offline-package-test",
                "script_name": str(ROOT / "setup.py"),
                "packages": ["pyclaude", *find_packages(str(SDK_SOURCE))],
                "package_dir": {"pyclaude": str(ROOT / "claude/pyclaude"),
                                "claude_agent_sdk": str(SDK_SOURCE / "claude_agent_sdk")},
                "package_data": {"claude_agent_sdk": ["py.typed", "_bundled/claude"]},
            })
            builder: Final = setup["BuildPyNative"](distribution)
            builder.ensure_finalized()
            builder.build_lib = str(stage)
            builder.compile = False
            builder.force = True
            with patch.object(builder, "_build_antigravity_shim"), \
                    patch.object(builder, "_build_codex"), \
                    patch.object(builder, "_build_kimi"), \
                    patch.object(builder, "_bundle_artifacts"), \
                    patch("subprocess.run") as native_build:
                builder.run()
            native_build.assert_called_once_with(
                [sys.executable, str(BUILD_SCRIPT)], cwd=str(ROOT), check=True)
            bundled: Final = stage / "claude_agent_sdk/_bundled/claude"
            self.assertTrue(helpers["matches"](bundled, pin))
            self.assertTrue(os.access(bundled, os.X_OK))
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
            self.assertEqual(json.loads(probe.stdout),
                             [str(stage / "claude_agent_sdk"), pin["version"]])
            version: Final = subprocess.run([str(bundled), "--version"], cwd=sandbox,
                                            env=env, text=True, capture_output=True,
                                            check=True, timeout=30)
            self.assertIn(pin["version"], version.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
