#!/usr/bin/env python3
"""Offline checks for redistributable bundles and the reconstruct helper.

setup.py's two build modes run with stubbed native steps; the helper's pin checks, launcher
resolution, candidate search, cache seeding, target probing and installation run against
synthetic files. Nothing is built, downloaded or launched beyond a Python probe.
Run with ``python test_scripts/test_reconstruct.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import patch

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tasksolver import reconstruct
from tasksolver.reconstruct import (
    LOCAL_ONLY,
    Pin,
    ReconstructError,
    agy_candidates,
    agy_pin,
    claude_candidates,
    claude_pin,
    expand,
    install_agy,
    install_claude,
    mismatch,
    resolve_target,
    sdk_version,
    seed,
    select,
)

if TYPE_CHECKING:
    from setup import BuildPyNative


def _pin(directory: Path, content: bytes, runtime: str = "claude") -> Pin:
    return Pin(runtime, (runtime,), "9.9.9", len(content), "sha256", hashlib.sha256(content).hexdigest(),
               directory / "cache" / f"{runtime}-pinned", 0o600)


def _write(path: Path, content: bytes | str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)
    return path


class PinTests(unittest.TestCase):
    def test_agy_pin_matches_the_cmake_configuration(self) -> None:
        pin = agy_pin(ROOT)
        document = cast(dict[str, dict[str, object]], json.loads((ROOT / "antigravity/vendor.json").read_text()))
        cli = document["cli"]
        self.assertEqual((pin.version, pin.size, pin.digest), (cli["version"], cli["size"], cli["sha512"]))
        self.assertEqual(pin.cache, ROOT / "antigravity/build" / f"antigravity-{cli['archive_sha512']}.raw")
        cmake = (ROOT / "antigravity/CMakeLists.txt").read_text()
        for field in ("version", "url", "archive_sha512", "sha512", "size"):
            self.assertIn(f'"${{_agy_pin}}" cli {field})', cmake)
        self.assertIn('set(AGY_RAW "${CMAKE_BINARY_DIR}/antigravity-${AGY_SHA512}.raw")', cmake)

    def test_claude_pin_matches_build_cli(self) -> None:
        from claude.build_cli import CLIPin, _load_pin, fetch_cli

        pin = claude_pin(ROOT)
        expected = _load_pin(ROOT / "claude/vendor.json")
        self.assertEqual((pin.version, pin.size, pin.digest),
                         (expected["version"], expected["size"], expected["sha256"]))

        def cached(target: Path, _pin: CLIPin) -> Path:
            return target

        with patch("claude.build_cli._download", side_effect=cached):
            self.assertEqual(fetch_cli(ROOT / "claude", expected), pin.cache)

    def test_mismatch_and_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            right = _write(root / "right", b"pinned executable")
            same_size = _write(root / "same", b"pinned executablX")
            other_size = _write(root / "other", b"another build")
            pin = _pin(root, b"pinned executable")
            self.assertIsNone(mismatch(right, pin))
            self.assertIn("differs", mismatch(same_size, pin) or "")
            self.assertIn("size", mismatch(other_size, pin) or "")
            reports: list[str] = []
            chosen = select(pin, None, [root / "missing", other_size, same_size, right], reports.append)
            self.assertEqual(chosen, right.resolve())
            self.assertEqual(len(reports), 2)
            with self.assertRaisesRegex(ReconstructError, "not the pinned claude 9.9.9 build"):
                _ = select(pin, same_size, [right], reports.append)
            with self.assertRaisesRegex(ReconstructError, "pass --claude PATH"):
                _ = select(pin, None, [other_size], reports.append)


class LauncherTests(unittest.TestCase):
    def test_nix_style_binary_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = _write(root / "store/claude-code/bin/.claude-wrapped", b"pinned executable", 0o755)
            loader = _write(root / "store/glibc/lib/ld-linux-x86-64.so.2", b"loader", 0o755)
            wrapper = _write(root / "profile/bin/claude",
                             b"\x7fELF\x00" + bytes(real) + b"\x00" + bytes(loader) + b"\x00", 0o755)
            self.assertEqual(expand(wrapper, ("claude",)), [wrapper.resolve(), real.resolve()])
            self.assertEqual(select(_pin(root, b"pinned executable"), wrapper, [], print), real.resolve())

    def test_exec_script_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = _write(root / "libexec/antigravity-cli/agy", b"agy", 0o755)
            script = _write(root / "bin/agy", f'#!/bin/sh\nexec -a "$0" "{real}" "$@"\n', 0o755)
            self.assertEqual(expand(script, ("agy", "antigravity")), [script.resolve(), real.resolve()])

    def test_common_locations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            empty_path = home / "empty"
            empty_path.mkdir()
            agy = _pin(home, b"agy 1.0", "agy")
            _ = _write(home / ".local/bin/agy", b"agy 2.0 self-updated", 0o755)
            backup = _write(home / ".local/bin/agy.1783000333329621781.old", b"agy 1.0", 0o755)
            claude = _pin(home, b"claude 9.9.9")
            _ = _write(home / ".local/share/claude/versions/9.9.8", b"claude 9.9.8", 0o755)
            extension = _write(home / ".vscode-server/extensions/anthropic.claude-code-9.9.9-linux-x64"
                                      "/resources/native-binary/claude", b"claude 9.9.9", 0o755)
            reports: list[str] = []
            with patch.dict(os.environ, {"PATH": str(empty_path)}):
                self.assertEqual(select(agy, None, agy_candidates(agy, home), reports.append), backup.resolve())
                self.assertEqual(select(claude, None, claude_candidates(claude, home), reports.append),
                                 extension.resolve())
            self.assertEqual(len(reports), 2)


class SeedAndInstallTests(unittest.TestCase):
    def test_seed_copies_the_verified_input_into_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "installed/claude", b"pinned executable", 0o555)
            pin = _pin(root, b"pinned executable")
            seed(pin, source)
            self.assertEqual(pin.cache.read_bytes(), b"pinned executable")
            self.assertEqual(stat.S_IMODE(pin.cache.stat().st_mode), 0o600)
            seed(pin, pin.cache.resolve())       # already the cache: nothing to copy
            self.assertEqual([path.name for path in pin.cache.parent.iterdir()], [pin.cache.name])

    def test_install_agy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("agy", "libagy.so"):
                _ = _write(root / "checkout/antigravity/vendor" / name, name.encode(), 0o775)
            pyagy = root / "site/pyagy"
            pyagy.mkdir(parents=True)
            install_agy(root / "checkout", pyagy)
            for name in ("agy", "libagy.so"):
                self.assertEqual((pyagy / "vendor" / name).read_bytes(), name.encode())
                self.assertEqual(stat.S_IMODE((pyagy / "vendor" / name).stat().st_mode), 0o755)

    def test_install_claude_swaps_only_a_verified_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            built = root / "checkout" / reconstruct.SDK_DIR / "_bundled"
            _ = _write(built / "claude", b"launcher", 0o755)
            _ = _write(built / "source/root/cli", b"cli")
            sdk = root / "site/claude_agent_sdk"
            _ = _write(sdk / "_bundled/stale", b"previous runtime")

            def rejected(_runtime: Path) -> bool:
                return False

            with self.assertRaises(ReconstructError):
                install_claude(root / "checkout", sdk, rejected)
            self.assertTrue((sdk / "_bundled/stale").is_file())
            install_claude(root / "checkout", sdk, lambda runtime: (runtime / "claude").is_file())
            self.assertFalse((sdk / "_bundled/stale").exists())
            self.assertEqual((sdk / "_bundled/source/root/cli").read_bytes(), b"cli")
            self.assertTrue(os.access(sdk / "_bundled/claude", os.X_OK))
            self.assertEqual(sorted(path.name for path in sdk.iterdir()), ["_bundled"])


class TargetTests(unittest.TestCase):
    def test_site_packages_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "lib/python3.15/site-packages"
            (site / "pyagy").mkdir(parents=True)
            target = resolve_target(str(site))
            self.assertEqual((target.python, target.pyagy, target.claude_sdk), ((3, 15), site / "pyagy", None))

    def test_probe_of_this_environment(self) -> None:
        target = resolve_target(None)
        self.assertEqual(target.python, (sys.version_info.major, sys.version_info.minor))
        self.assertIsNotNone(target.pyagy)
        self.assertIsNotNone(target.claude_sdk)

    def test_sdk_version_is_read_without_importing(self) -> None:
        sdk = cast(dict[str, dict[str, str]], json.loads((ROOT / "claude/vendor.json").read_text()))["sdk"]["tag"]
        self.assertEqual("v" + str(sdk_version(ROOT / reconstruct.SDK_DIR)), sdk)

    def test_target_must_match_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site-packages"
            _ = _write(site / "pyagy" / reconstruct.BOOKS, "BOOKS = ()\n")
            _ = _write(site / "claude_agent_sdk/_version.py", '__version__ = "0.0.1"\n')
            target = resolve_target(str(site))
            with self.assertRaisesRegex(ReconstructError, "book table"):
                _ = reconstruct.agy_job(ROOT, target, None)
            with self.assertRaisesRegex(ReconstructError, "is Agent SDK 0.0.1"):
                _ = reconstruct.claude_job(ROOT, target, None)


class MainTests(unittest.TestCase):
    def test_restores_both_runtimes_into_a_bundle_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            agy_bytes, claude_bytes = b"official agy", b"official claude"
            _ = _write(checkout / "antigravity/CMakeLists.txt", "")
            _ = _write(checkout / "claude/build_cli.py", "")
            _ = _write(checkout / "antigravity/vendor.json", json.dumps({"cli": {
                "version": "1.0.0", "url": "https://example.invalid/agy.tar.gz", "archive_sha512": "a" * 128,
                "sha512": hashlib.sha512(agy_bytes).hexdigest(), "size": len(agy_bytes)}}))
            _ = _write(checkout / "claude/vendor.json", json.dumps({"cli": {
                "version": "9.9.9", "url": "https://example.invalid/claude",
                "sha256": hashlib.sha256(claude_bytes).hexdigest(), "size": len(claude_bytes)}}))
            _ = _write(checkout / reconstruct.PYAGY_DIR / reconstruct.BOOKS, "BOOKS = ()\n")
            for name in ("agy", "libagy.so"):
                _ = _write(checkout / "antigravity/vendor" / name, name.encode(), 0o755)
            _ = _write(checkout / reconstruct.SDK_DIR / "_bundled/claude", b"launcher", 0o755)
            _ = _write(checkout / reconstruct.SDK_DIR / "_version.py", '__version__ = "0.9.9"\n')
            site = root / f"env/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            _ = _write(site / "pyagy" / reconstruct.BOOKS, "BOOKS = ()\n")
            _ = _write(site / "claude_agent_sdk/_version.py", '__version__ = "0.9.9"\n')
            agy = _write(root / "downloads/agy", agy_bytes, 0o755)
            claude = _write(root / "downloads/claude", claude_bytes, 0o755)

            def valid(_checkout: Path) -> Callable[[Path], bool]:
                return lambda runtime: (runtime / "claude").is_file()

            with patch.dict(os.environ, {"CONDA_PREFIX": str(root / "env")}), \
                    patch("shutil.which", return_value="/usr/bin/true"), \
                    patch("tasksolver.reconstruct.build_agy") as build_agy, \
                    patch("tasksolver.reconstruct.build_claude") as build_claude, \
                    patch("tasksolver.reconstruct.claude_validator", side_effect=valid):
                status = reconstruct.main(["--agy", str(agy), f"--claude={claude}", "--target", str(site),
                                           "--source", str(checkout)])
            self.assertEqual(status, 0)
            build_agy.assert_called_once()
            build_claude.assert_called_once()
            self.assertEqual((checkout / "antigravity/build" / f"antigravity-{'a' * 128}.raw").read_bytes(),
                             agy_bytes)
            self.assertEqual((checkout / "claude/build/cache" /
                              f"claude-{hashlib.sha256(claude_bytes).hexdigest()}").read_bytes(), claude_bytes)
            self.assertEqual((site / "pyagy/vendor/libagy.so").read_bytes(), b"libagy.so")
            self.assertEqual((site / "claude_agent_sdk/_bundled/claude").read_bytes(), b"launcher")

    def test_installed_copy_without_sources_names_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(reconstruct, "__file__", str(Path(directory) / "tasksolver/reconstruct.py")):
            with self.assertRaisesRegex(ReconstructError, "pixi run -m <checkout>/pyproject.toml reconstruct"):
                _ = reconstruct.find_source(None)


class BundleModeTests(unittest.TestCase):
    """setup.py's redistributable and local modes, with every native step stubbed."""

    setup_namespace: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        with patch("setuptools.setup"):
            cls.setup_namespace = cast(dict[str, object], runpy.run_path(str(ROOT / "setup.py")))

    def test_rule(self) -> None:
        rule = cast(Callable[[bool, Mapping[str, str]], bool], self.setup_namespace["redistributable_bundle"])
        self.assertTrue(rule(False, {"CONDA_BUILD": "1"}))
        self.assertFalse(rule(True, {"CONDA_BUILD": "1"}))
        self.assertFalse(rule(False, {}))

    def _build(self, root: Path, environment: Mapping[str, str], planted: Path | None = None) -> tuple[Path, dict[str, int]]:
        from setuptools import Distribution

        sdk = root / "sdk/claude_agent_sdk"
        _ = _write(sdk / "__init__.py", "")
        _ = _write(sdk / "_bundled/claude", b"launcher", 0o755)
        _ = _write(sdk / "_bundled/source/root/cli", b"cli")
        artifacts = root / "artifacts"
        jobs = [(str(_write(artifacts / name, name.encode(), 0o755)), package, name, False)
                for package, name in (("pyagy", "agy"), ("pyagy", "libagy.so"), ("pycodex", "codex"),
                                      ("pykimi", "main.mjs"))]
        stage = root / "wheel"
        if planted is not None:
            _ = _write(stage / planted, b"left by an earlier build")
        distribution = Distribution({
            "name": "tasksolver-bundle-mode-test", "script_name": str(ROOT / "setup.py"),
            "packages": ["claude_agent_sdk"], "package_dir": {"claude_agent_sdk": str(sdk)},
            "package_data": {"claude_agent_sdk": ["_bundled/claude", "_bundled/source/**/*"]}})
        builder_type = cast("type[BuildPyNative]", self.setup_namespace["BuildPyNative"])
        builder = builder_type(distribution)
        builder.ensure_finalized()
        builder.build_lib = str(stage)
        builder.compile = False
        builder.force = True
        steps = ("_build_claude", "_build_bridge", "_build_antigravity_shim", "_build_codex", "_build_kimi")
        with patch.dict(os.environ, environment), \
                patch.object(builder, "_bundle_jobs", return_value=jobs), \
                patch.object(builder, steps[0]) as claude, patch.object(builder, steps[1]) as bridge, \
                patch.object(builder, steps[2]) as agy, patch.object(builder, steps[3]) as codex, \
                patch.object(builder, steps[4]) as kimi:
            builder.run()
        calls = {name: mock.call_count for name, mock in zip(steps, (claude, bridge, agy, codex, kimi))}
        return stage, calls

    def test_redistributable_build_leaves_local_only_runtimes_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage, calls = self._build(root, {"CONDA_BUILD": "1", "PREFIX": str(root / "host")})
            self.assertEqual(calls, {"_build_claude": 0, "_build_bridge": 1, "_build_antigravity_shim": 0,
                                     "_build_codex": 1, "_build_kimi": 1})
            self.assertTrue((stage / "claude_agent_sdk/__init__.py").is_file())
            self.assertTrue((stage / "pycodex/vendor/codex").is_file())
            self.assertTrue((stage / "pykimi/vendor/main.mjs").is_file())
            for path in LOCAL_ONLY:
                self.assertFalse((stage / path).exists(), path)

    def test_local_build_keeps_everything(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {key: value for key, value in os.environ.items() if key != "CONDA_BUILD"}
            with patch.dict(os.environ, environment, clear=True):
                stage, calls = self._build(Path(directory), {})
            self.assertEqual(set(calls.values()), {1})
            for path in LOCAL_ONLY:
                self.assertTrue((stage / path).exists(), path)

    def test_a_leaked_runtime_fails_the_redistributable_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(SystemExit, "still contains pyagy/vendor/agy"):
                _ = self._build(root, {"CONDA_BUILD": "1", "PREFIX": str(root / "host")},
                                planted=Path("pyagy/vendor/agy"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
