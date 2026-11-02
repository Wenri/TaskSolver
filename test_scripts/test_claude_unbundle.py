#!/usr/bin/env python3
"""Offline checks for Claude's split runtime, source, assets and fresh caches."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast
from unittest.mock import patch

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claude.build_cli import CLIPin, _load_pin, matches
from claude.unbundle import _read_graph, _source_only, unbundle
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from elftools.elf.elffile import ELFFile
from pyclaude.client import PARENT_SESSION_ENV
from wirecap.types import json_array, json_loads_object, json_object


@dataclass(frozen=True)
class ModuleFixture:
    name: str
    content: bytes
    loader: int = 1
    encoding: int = 1


@dataclass(frozen=True)
class GraphFixture:
    data: bytes
    section: int
    footer: int
    records: tuple[int, ...]
    contents: tuple[tuple[int, int], ...]

    def replace_u32(self, offset: int, value: int) -> bytes:
        result = bytearray(self.data)
        struct.pack_into("<I", result, offset, value)
        return bytes(result)


def graph_fixture(modules: tuple[ModuleFixture, ...] | None = None) -> GraphFixture:
    """Build a minimal ELF independently of the production graph reader."""
    if modules is None:
        modules = (
            ModuleFixture("/$bunfs/root/cli", b"// @bun @bytecode\n" + b" " * 1024),
            ModuleFixture("/$bunfs/root/helper.js", b"export const helper = 1;\n"),
            ModuleFixture("/$bunfs/root/asset.bin", b"\x00/$bunfs/root/literal\xff", 5, 0),
        )
    graph = bytearray()
    rows: list[bytes] = []
    contents: list[tuple[int, int]] = []

    def append(value: bytes) -> tuple[int, int]:
        offset = len(graph)
        graph.extend(value)
        graph.append(0)
        return offset, len(value)

    for module in modules:
        name = append(module.name.encode())
        content = append(module.content)
        contents.append(content)
        bytecode = append(b"bytecode:" + module.name.encode())
        info = append(b"module-info:" + module.name.encode())
        origin = append(module.name.encode())
        rows.append(struct.pack(
            "<12I4B", *name, *content, 0, 0, *bytecode, *info, *origin,
            module.encoding, module.loader, 1 if module.loader == 1 else 0,
            0 if module.loader == 1 else 1,
        ))
    directory = len(graph)
    graph.extend(b"".join(rows))
    footer = len(graph)
    graph.extend(struct.pack("<QIIIIII", footer, directory, len(rows) * 52, 0, footer, 0, 0))
    graph.extend(b"\n---- Bun! ----\n")
    section = struct.pack("<Q", len(graph)) + graph
    names = b"\x00.shstrtab\x00.bun\x00"
    section_offset = 128
    section_headers = (section_offset + len(section) + 7) & ~7
    elf = bytearray(section_headers + 3 * 64)
    elf[:64] = struct.pack(
        "<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\x00" * 9,
        3, 62, 1, 0, 0, section_headers, 0, 64, 0, 0, 64, 3, 1,
    )
    elf[64:64 + len(names)] = names
    elf[section_offset:section_offset + len(section)] = section
    struct.pack_into("<IIQQQQIIQQ", elf, section_headers + 64,
                     1, 3, 0, 0, 64, len(names), 0, 0, 1, 0)
    struct.pack_into("<IIQQQQIIQQ", elf, section_headers + 128,
                     11, 1, 0, 0, section_offset, len(section), 0, 0, 8, 0)
    base = section_offset + 8
    return GraphFixture(bytes(elf), section_offset, base + footer,
                        tuple(base + directory + index * 52 for index in range(len(rows))),
                        tuple((base + offset, size) for offset, size in contents))


def pin_for(data: bytes) -> CLIPin:
    return {"version": "fixture", "url": "https://example.invalid/unused",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class GraphEditTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tasksolver-claude-graph-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.fixture = graph_fixture()
        self.source = self.directory / "original"
        self.source.write_bytes(self.fixture.data)

    def test_extractor_copy_invalidates_every_cache_without_changing_source_or_assets(self) -> None:
        destination = self.directory / "source-only"
        _source_only(self.source, destination, _read_graph(self.source))
        expected = bytearray(self.fixture.data)
        for record in self.fixture.records:
            expected[record + 24:record + 40] = bytes(16)
        self.assertEqual(destination.read_bytes(), bytes(expected))
        self.assertEqual(self.source.read_bytes(), self.fixture.data)

    def test_malformed_directory_entry_and_content_ranges_are_rejected(self) -> None:
        cases = {
            "partial module": (self.fixture.footer + 12, 53),
            "entry outside directory": (self.fixture.footer + 16, 99),
            "directory outside graph": (self.fixture.footer + 8, len(self.fixture.data)),
            "content outside graph": (self.fixture.records[0] + 8, len(self.fixture.data)),
            "name outside graph": (self.fixture.records[0], len(self.fixture.data)),
            "content inside module table": (self.fixture.records[0] + 8,
                                            self.fixture.records[0] - self.fixture.section - 8),
        }
        for label, (offset, value) in cases.items():
            with self.subTest(label=label):
                self.source.write_bytes(self.fixture.replace_u32(offset, value))
                with self.assertRaises(ValueError):
                    _read_graph(self.source)

    def test_bad_magic_and_unknown_flags_are_rejected(self) -> None:
        damaged = bytearray(self.fixture.data)
        damaged[self.fixture.footer + 32] ^= 1
        for data in (bytes(damaged), self.fixture.replace_u32(self.fixture.footer + 28, 1 << 31)):
            self.source.write_bytes(data)
            with self.assertRaises(ValueError):
                _read_graph(self.source)

    def test_unknown_javascript_encoding_and_unterminated_contents_are_rejected(self) -> None:
        encoding = bytearray(self.fixture.data)
        encoding[self.fixture.records[0] + 48] = 2
        unterminated = bytearray(self.fixture.data)
        offset, length = self.fixture.contents[0]
        unterminated[offset + length] = 1
        for data in (bytes(encoding), bytes(unterminated)):
            self.source.write_bytes(data)
            with self.assertRaises(ValueError):
                _read_graph(self.source)

    def test_unsafe_duplicate_and_non_utf8_module_names_are_rejected(self) -> None:
        for name in ("/$bunfs/root/../outside", "/$bunfs/root/a//b", "/elsewhere/cli",
                     "/$bunfs/root/cli"):
            fixture = graph_fixture((ModuleFixture("/$bunfs/root/cli", b" " * 1024),
                                     ModuleFixture(name, b"export {};")))
            self.source.write_bytes(fixture.data)
            with self.subTest(name=name), self.assertRaises(ValueError):
                _read_graph(self.source)
        damaged = bytearray(self.fixture.data)
        damaged[self.fixture.section + 8] = 255
        self.source.write_bytes(damaged)
        with self.assertRaises(ValueError):
            _read_graph(self.source)

    def test_wrong_pin_rejected_before_output_is_created(self) -> None:
        output = self.directory / "output"
        pin = pin_for(self.fixture.data)
        pin["sha256"] = "0" * 64
        with self.assertRaises((ValueError, RuntimeError)):
            unbundle(output, binary=self.source, bun_graph=sys.executable, pin=pin)
        self.assertFalse(output.exists())
        self.assertEqual(self.source.read_bytes(), self.fixture.data)

    def test_missing_external_extractor_leaves_no_output(self) -> None:
        output = self.directory / "output"
        with patch("claude.unbundle.shutil.which", return_value=None) as locate:
            with self.assertRaisesRegex(FileNotFoundError, "bun_graph"):
                unbundle(output, binary=self.source, pin=pin_for(self.fixture.data))
        locate.assert_called_once_with("bun_graph")
        self.assertFalse(output.exists())
        self.assertEqual(self.source.read_bytes(), self.fixture.data)

    def test_existing_outputs_including_dangling_symlinks_are_preserved(self) -> None:
        output = self.directory / "output"
        output.mkdir()
        marker = output / "keep"
        marker.write_text("existing")
        with self.assertRaises((ValueError, FileExistsError)):
            unbundle(output, binary=self.source, bun_graph=sys.executable, pin=pin_for(self.fixture.data))
        self.assertEqual(marker.read_text(), "existing")
        marker.unlink()
        output.rmdir()
        output.symlink_to(self.directory / "absent")
        with self.assertRaises((ValueError, FileExistsError)):
            unbundle(output, binary=self.source, bun_graph=sys.executable, pin=pin_for(self.fixture.data))
        self.assertTrue(output.is_symlink())

    def test_extractor_failure_removes_partial_output_and_keeps_original(self) -> None:
        extractor = self.directory / "failed-extractor"
        extractor.write_text("#!/bin/sh\nexit 23\n")
        extractor.chmod(0o700)
        output = self.directory / "output"
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            unbundle(output, binary=self.source, bun_graph=str(extractor), pin=pin_for(self.fixture.data))
        self.assertEqual(caught.exception.returncode, 23)
        self.assertFalse(output.exists())
        self.assertEqual(self.source.read_bytes(), self.fixture.data)

    def test_extractor_symlink_is_rejected_without_touching_its_target(self) -> None:
        target = self.directory / "keep"
        target.write_text("untouched")
        extractor = self.directory / "unsafe-extractor"
        extractor.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys\n"
            "root = pathlib.Path(sys.argv[-1]) / 'root'\n"
            "root.mkdir()\n"
            f"(root / 'cli').symlink_to({str(target)!r})\n"
        )
        extractor.chmod(0o700)
        output = self.directory / "output"
        with self.assertRaisesRegex(ValueError, "Unexpected bun_graph output"):
            unbundle(output, binary=self.source, bun_graph=str(extractor), pin=pin_for(self.fixture.data))
        self.assertFalse(output.exists())
        self.assertEqual(target.read_text(), "untouched")

    def test_unsafe_output_path_is_rejected_before_extraction(self) -> None:
        for character in ("'", '"', "\\", "\n", "`", "$"):
            output = self.directory / ("output" + character)
            with self.subTest(character=character), self.assertRaises(ValueError):
                unbundle(output, binary=self.source, bun_graph=sys.executable, pin=pin_for(self.fixture.data))
            self.assertFalse(output.exists())


class PinnedRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pin = _load_pin(ROOT / "claude/vendor.json")
        cls.binary = ROOT / "claude/build/cache" / f"claude-{cls.pin['sha256']}"
        if not cls.binary.is_file():
            raise unittest.SkipTest("Build the pinned Claude executable before native unbundle checks")
        cls.temporary = tempfile.TemporaryDirectory(prefix="tasksolver-claude-unbundle-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        built = unbundle(cls.directory / "output", binary=cls.binary, pin=cls.pin)
        relocated = cls.directory / "relocated package # source"
        built.runtime.parent.rename(relocated)
        cls.package = relocated
        cls.runtime = relocated / built.runtime.name
        cls.entrypoint = relocated / built.entrypoint.relative_to(built.runtime.parent)
        cls.bun = relocated / "bun"
        cls.source = relocated / "source"
        cls.assets = relocated / "assets"
        cls.modules = relocated / "modules"
        assert not built.runtime.parent.exists()

    def run_runtime(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.runtime), *args], cwd=self.directory,
                              env=dict(os.environ, DISABLE_AUTOUPDATER="1"),
                              capture_output=True, text=True, timeout=30)

    def test_version_and_help_use_external_graph_with_original_binary_intact(self) -> None:
        version = self.run_runtime("--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn(self.pin["version"], version.stdout)
        help_result = self.run_runtime("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--output-format", help_result.stdout)
        self.assertTrue(matches(self.binary, self.pin))

    def test_edited_entrypoint_executes_with_private_native_cells(self) -> None:
        original = self.entrypoint.read_bytes()
        self.addCleanup(self.entrypoint.write_bytes, original)
        self.entrypoint.write_text(
            'if (process.env.DISABLE_AUTOUPDATER !== "1") throw new Error("updater can replace bundled runtime");\n'
            'if (typeof Bun.ant?.CellSegmenter !== "function") throw new Error("missing native cells");\n'
            'console.log("external graph with native cells");\n'
        )
        probe = subprocess.run([str(self.runtime)], cwd=self.directory,
                               env=dict(os.environ, DISABLE_AUTOUPDATER="0"),
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "external graph with native cells")

    def test_launcher_preserves_disabled_project_configuration_autoloading(self) -> None:
        workspace = self.directory / "project-with-bun-config"
        workspace.mkdir()
        (workspace / "bunfig.toml").write_text('preload = ["./preload.mjs"]\n')
        (workspace / "preload.mjs").write_text('process.env.TASKSOLVER_UNEXPECTED_PRELOAD="1";\n')
        (workspace / ".env").write_text("TASKSOLVER_UNEXPECTED_DOTENV=1\n")
        original = self.entrypoint.read_bytes()
        self.addCleanup(self.entrypoint.write_bytes, original)
        self.entrypoint.write_text(
            'if(process.env.TASKSOLVER_UNEXPECTED_PRELOAD||process.env.TASKSOLVER_UNEXPECTED_DOTENV)'
            'throw Error("project configuration was loaded");\nconsole.log("configuration ignored");\n'
        )
        env = dict(os.environ)
        env.pop("TASKSOLVER_UNEXPECTED_PRELOAD", None)
        env.pop("TASKSOLVER_UNEXPECTED_DOTENV", None)
        result = subprocess.run([str(self.runtime)], cwd=workspace, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "configuration ignored")

    def test_clean_runtime_preserves_native_sections_without_embedded_graph(self) -> None:
        manifest = json_loads_object((self.package / "manifest.json").read_text())
        patches = json_array(manifest["native_patches"])
        self.assertEqual(len(patches), 1)
        patch = json_object(patches[0])
        address, target_address = patch["address"], patch["target"]
        original_hex, replacement_hex = patch["original"], patch["replacement"]
        self.assertEqual(patch["name"], "external_source_utf8")
        self.assertEqual(patch["section"], ".tasksolver.utf8")
        assert isinstance(address, int) and isinstance(target_address, int)
        assert isinstance(original_hex, str) and isinstance(replacement_hex, str)
        original_call, replacement_call = bytes.fromhex(original_hex), bytes.fromhex(replacement_hex)
        self.assertEqual(address, 0x69930AB)
        self.assertEqual(len(original_call), 5)
        self.assertEqual(len(replacement_call), 5)
        self.assertEqual(original_call[0], 0xE8)
        self.assertEqual(replacement_call[0], 0xE8)
        self.assertEqual(address + 5 + struct.unpack("<i", original_call[1:])[0], 0x7E6D0A0)
        self.assertEqual(address + 5 + struct.unpack("<i", replacement_call[1:])[0], target_address)
        with self.binary.open("rb") as original, self.bun.open("rb") as runtime:
            before, after = ELFFile(original), ELFFile(runtime)
            self.assertIsNone(after.get_section_by_name(".bun"))
            for kind in ("PT_GNU_RELRO", "PT_GNU_STACK"):
                old_segments = [segment for segment in before.iter_segments() if segment["p_type"] == kind]
                new_segments = [segment for segment in after.iter_segments() if segment["p_type"] == kind]
                self.assertEqual(len(old_segments), 1, kind)
                self.assertEqual(len(new_segments), 1, kind)
                for field in ("p_vaddr", "p_offset", "p_filesz", "p_memsz", "p_flags", "p_align"):
                    self.assertEqual(new_segments[0][field], old_segments[0][field], (kind, field))
            embedded = before.get_section_by_name(".bun")
            assert embedded is not None
            original_payload_size = embedded["sh_size"]
            old_names = {section.name for section in before.iter_sections()}
            added = [section for section in after.iter_sections()
                     if section["sh_flags"] & 2 and section.name not in old_names]
            self.assertEqual(len(added), 1)
            self.assertEqual(added[0].name, patch["section"])
            self.assertEqual(added[0]["sh_flags"], 6)
            self.assertTrue(added[0]["sh_addr"] <= target_address
                            < added[0]["sh_addr"] + added[0]["sh_size"])
            target_mapped = False
            for segment in after.iter_segments():
                if segment["p_type"] == "PT_LOAD":
                    self.assertFalse(segment["p_vaddr"] <= embedded["sh_addr"]
                                     < segment["p_vaddr"] + segment["p_memsz"])
                    if segment["p_vaddr"] <= target_address < segment["p_vaddr"] + segment["p_memsz"]:
                        self.assertEqual(segment["p_flags"], 5)
                        target_mapped = True
            self.assertTrue(target_mapped)
            compared = 0
            patched = False
            for section in before.iter_sections():
                if section.name == ".bun" or not section["sh_flags"] & 2:
                    continue
                target = after.get_section_by_name(section.name)
                self.assertIsNotNone(target, section.name)
                assert target is not None
                for field in ("sh_addr", "sh_size", "sh_flags"):
                    self.assertEqual(target[field], section[field], (section.name, field))
                if section["sh_type"] != "SHT_NOBITS":
                    expected = bytearray(section.data())
                    if section["sh_addr"] <= address < section["sh_addr"] + section["sh_size"]:
                        offset = address - section["sh_addr"]
                        self.assertEqual(expected[offset:offset + 5], original_call)
                        expected[offset:offset + 5] = replacement_call
                        patched = True
                    self.assertEqual(hashlib.sha256(target.data()).digest(),
                                     hashlib.sha256(expected).digest(), section.name)
                compared += 1
            self.assertGreater(compared, 10)
            self.assertTrue(patched)
        self.assertFalse((self.package / "graph").exists())
        self.assertFalse((self.package / ".build").exists())
        self.assertFalse((self.package / "libwire.so").exists())
        self.assertLess(self.runtime.stat().st_size, 1024 * 1024)
        self.assertLessEqual(self.bun.stat().st_size,
                             self.binary.stat().st_size - original_payload_size + 65536)
        probe = subprocess.run(
            [str(self.bun), "-e", "console.log(Bun.isStandaloneExecutable,typeof Bun.ant?.CellSegmenter)"],
            cwd=self.directory, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "false function")

    def test_all_external_assets_preserve_file_and_text_module_semantics(self) -> None:
        graph = _read_graph(self.binary)
        records: list[dict[str, str | int]] = []
        with self.binary.open("rb") as source:
            for module in graph.modules:
                if module.loader == 1:
                    self.assertTrue((self.source / module.relative_path).is_file(), module.name)
                    continue
                source.seek(module.contents_offset)
                raw = source.read(module.contents_length)
                source.seek(module.record_offset + 48)
                encoding = source.read(1)[0]
                expected = raw if encoding == 0 else raw.decode(
                    "utf-16-le" if encoding == 2 else "latin-1").encode()
                path = (self.modules if module.loader == 10 else self.assets) / module.relative_path
                self.assertEqual(path.read_bytes(), expected, module.name)
                records.append({"name": module.name, "path": str(path), "loader": module.loader,
                                "sha256": hashlib.sha256(expected).hexdigest()})
        self.assertEqual(len(records), 232)
        self.assertEqual(sum(record["loader"] == 13 for record in records), 98)
        script = self.directory / "all-assets.mjs"
        script.write_text(
            f'import {json.dumps((self.source / "tasksolver-resolver.mjs").as_uri())};\n'
            f'const assets={json.dumps(records)};\n'
            'for(const asset of assets){\n'
            ' const bytes=await Bun.file(asset.path).arrayBuffer();\n'
            ' if(Bun.CryptoHasher.hash("sha256",bytes,"hex")!==asset.sha256)throw Error(asset.name);\n'
            ' if(asset.loader===13){\n'
            '  const text=import.meta.require(asset.name);\n'
            '  if(typeof text!=="string"||text!==(await import(asset.name)).default||\n'
            '     Bun.CryptoHasher.hash("sha256",text,"hex")!==asset.sha256)throw Error(asset.name);\n'
            ' }\n}\n'
            'const clipboard=import.meta.require("/$bunfs/root/clipboard-napi.node");\n'
            'if(typeof clipboard.getLinuxClipboardText!=="function"||\n'
            '   typeof clipboard.setLinuxClipboardText!=="function")throw Error("native addon exports");\n'
            'console.log("232 assets, 98 text modules");\n'
        )
        result = subprocess.run([str(self.bun), str(script)], cwd=self.directory,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "232 assets, 98 text modules")

    def test_native_applets_keep_original_dispatch(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            original = Path(directory) / "original"
            shutil.copyfile(self.binary, original)
            original.chmod(0o700)
            for applet in ("rg", "ug", "ugrep", "bfs", "apply-seccomp"):
                with self.subTest(applet=applet):
                    before = subprocess.run([applet, "--version"], executable=original,
                                            capture_output=True, timeout=10)
                    after = subprocess.run([applet, "--version"], executable=self.runtime,
                                           capture_output=True, timeout=10)
                    self.assertEqual((after.returncode, after.stdout, after.stderr),
                                     (before.returncode, before.stdout, before.stderr))

    def test_fresh_unicode_cache_survives_relocation_and_rejects_stale_source(self) -> None:
        directory = self.directory / "cache-source"
        directory.mkdir()
        source = directory / "value.mjs"
        source.write_text(
            'export const value=42; const cooked="café 世界 😀";\n'
            'const raw=String.raw`café 世界 😀 \\u00e9`;\n'
            'console.log(JSON.stringify({value,cooked,raw,location:import.meta.url}));\n', encoding="utf-8")
        jobs = self.directory / "cache-jobs.json"
        jobs.write_text(json.dumps([str(source)]))
        generated = subprocess.run(
            [str(self.bun), "--no-env-file", "--config=/dev/null", "--tsconfig-override=/dev/null",
             "--no-install", str(self.package / "cache.mjs"), str(self.package / "libwire.node"), str(jobs)],
            cwd=self.directory, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        cache = source.with_name(source.name + ".jsc")
        self.assertTrue(cache.is_file())
        relocated = self.directory / "cache relocated # source"
        directory.rename(relocated)
        source = relocated / source.name
        cache = relocated / cache.name
        env = dict(os.environ, BUN_JSC_verboseDiskCache="1")
        accepted = subprocess.run([str(self.bun), str(source)], cwd=self.directory,
                                  env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        expected = {"value": 42, "cooked": "café 世界 😀", "raw": "café 世界 😀 \\u00e9",
                    "location": source.as_uri()}
        self.assertEqual(json_loads_object(accepted.stdout), expected)
        self.assertIn("Cache hit", accepted.stderr)
        original = source.read_bytes()
        self.assertEqual(original.count(b"value=42"), 1)
        fresh_cache = cache.read_bytes()
        source.write_bytes(original.replace(b"value=42", b"value=43"))
        self.assertEqual(source.stat().st_size, len(original))
        self.assertEqual(cache.read_bytes(), fresh_cache)
        stale = subprocess.run([str(self.bun), str(source)], cwd=self.directory,
                               env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(stale.returncode, 0, stale.stderr)
        self.assertEqual(json_loads_object(stale.stdout), {**expected, "value": 43})
        self.assertIn("Cache miss", stale.stderr)
        self.assertNotIn("Cache hit", stale.stderr)

    def test_native_cache_addon_rejects_foreign_runtime(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "The offline native boundary check requires Node on PATH")
        assert node is not None
        script = (
            'const addon=require(process.argv[1]);\n'
            'try{addon.emitModule("/tmp/unused-cache-source.mjs",Buffer.from("export const value=42;"));}\n'
            'catch(error){if(!String(error).includes("signature mismatch"))throw error;'
            'console.log("foreign runtime rejected");process.exit(0);}\n'
            'throw Error("private Bun emitter accepted a foreign runtime");\n'
        )
        result = subprocess.run([node, "-e", script, str(self.package / "libwire.node")],
                                cwd=self.directory, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "foreign runtime rejected")

    def test_worker_uses_editable_source_after_relocation(self) -> None:
        worker = self.source / "root/src/plugins/functionHooks/hooks-worker/hooks-worker.js"
        original = worker.read_bytes()
        entry = self.entrypoint.read_bytes()
        self.addCleanup(worker.write_bytes, original)
        self.addCleanup(self.entrypoint.write_bytes, entry)
        worker.write_text('postMessage("editable worker");close();\n')
        self.entrypoint.write_text(
            'const worker=new Worker(new URL("./src/plugins/functionHooks/hooks-worker/hooks-worker.js",import.meta.url));\n'
            'const timeout=setTimeout(()=>{worker.terminate();process.exitCode=1},5000);\n'
            'worker.onmessage=({data})=>{console.log(data);clearTimeout(timeout);worker.terminate()};\n'
            'worker.onerror=(error)=>{console.error(error);clearTimeout(timeout);process.exitCode=1};\n'
        )
        result = self.run_runtime()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "editable worker")

    def test_original_worker_initializes_through_external_resolver(self) -> None:
        original = self.entrypoint.read_bytes()
        self.addCleanup(self.entrypoint.write_bytes, original)
        self.entrypoint.write_text(
            'const worker=new Worker(new URL("./src/plugins/functionHooks/hooks-worker/hooks-worker.js",import.meta.url));\n'
            'const timeout=setTimeout(()=>{worker.terminate();process.exitCode=1},5000);\n'
            'worker.onmessage=({data})=>{console.log(data.type);clearTimeout(timeout);worker.terminate()};\n'
            'worker.onerror=(error)=>{console.error(error);clearTimeout(timeout);process.exitCode=1};\n'
        )
        result = self.run_runtime()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "online")

    def test_official_sdk_initializes_without_submitting_a_prompt(self) -> None:
        async def initialize() -> None:
            config = self.directory / "sdk-config"
            config.mkdir(exist_ok=True)
            env = {name: "" for name in PARENT_SESSION_ENV}
            env.update({"CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_API_KEY": "",
                        "CLAUDE_CODE_OAUTH_TOKEN": "", "DISABLE_AUTOUPDATER": "1",
                        "DISABLE_TELEMETRY": "1"})
            options = ClaudeAgentOptions(cli_path=self.runtime, cwd=self.directory,
                                         setting_sources=[], tools=[], env=env,
                                         extra_args={"strict-mcp-config": None, "no-session-persistence": None})
            async with asyncio.timeout(20):
                async with ClaudeSDKClient(options=options) as client:
                    info = cast(object, await client.get_server_info())
                    self.assertIsInstance(info, dict)
                    assert isinstance(info, dict)
                    self.assertIn("models", cast(dict[str, object], info))

        asyncio.run(initialize())


if __name__ == "__main__":
    unittest.main()
