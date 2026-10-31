#!/usr/bin/env python3
"""Offline native checks for the built agy stubs; no provider calls.

Run after ``pixi run build-shim`` with ``pixi run python test_scripts/test_agy_stubs.py``.
Missing build artifacts produce an explicit skip, not a passing native check.
"""
import collections
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

import lief

from pyagy import BOOKS, enabled_hooks
from pyagy._env import instrumented_env, preload_argv


ROOT = Path(__file__).resolve().parents[1]
ANTI = ROOT / "antigravity"
AGY = ANTI / "vendor/agy"
OBJECT = ANTI / "build/CMakeFiles/agy.dir/src/goframe.cpp.o"


class AgyStubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [p for p in (AGY, OBJECT, ANTI / "vendor/libagy.so", ANTI / "symbols/symbols.json")
                   if not p.is_file()]
        if missing:
            raise unittest.SkipTest(f"run pixi run build-shim; missing: {missing}")
        lief.logging.disable()
        cls.image = lief.parse(str(AGY))
        cls.obj = lief.parse(str(OBJECT))
        cls.go = lief.parse(str(ANTI / "vendor/libagy.so"))
        cls.symbols = {s.name: s for s in cls.image.symtab_symbols}
        cls.stubs = {name: s for name, s in cls.symbols.items() if name.startswith("agy_stub__")}
        cls.layout = {name: int(value) for name, value in re.findall(
            r"^#define\s+(AGY_\w+)\s+(\d+)\b", (ANTI / "src/goframe.h").read_text(), re.M)}
        cls.rows = {"agy_stub__" + row["id"]: (index, row) for index, row in enumerate(BOOKS)}

    def frame_hi(self, row):
        return self.layout["AGY_FRAME_HI_" + row["mech"].upper()]

    def test_slot_and_frame_geometry(self):
        expected = {"agy_stub__" + row["id"] for row in enabled_hooks()}
        self.assertEqual(set(self.stubs), expected)
        section = self.image.get_section("agy_trampolines")
        stride = self.layout["AGY_SLOT_SIZE"]
        self.assertEqual(section.size, len(expected) * stride)
        frame = struct.pack("b", self.layout["AGY_FRAME"])
        for index, stub in enumerate(sorted(self.stubs.values(), key=lambda s: s.value)):
            with self.subTest(stub=stub.name):
                self.assertEqual(stub.value, section.virtual_address + index * stride)
                self.assertEqual(stub.value % stride, 0)
                self.assertEqual(stub.size, stride)
                code = bytes(self.image.get_content_from_virtual_address(stub.value, stub.size))
                lo, hi = self.layout["AGY_FRAME_LO"], self.frame_hi(self.rows[stub.name][1])
                self.assertEqual(code[lo - 4:lo], b"\x48\x83\xec" + frame)
                self.assertEqual(code[hi - 5:hi], b"\x48\x8d\x64\x24" + frame)
                # CMP survives the flag-preserving stack restore for either verdict.
                action = self.layout["AGY_OFF_ACTION"]
                self.assertEqual(code[hi - 10:hi - 5], b"\x83\x7c\x24" + bytes([action, 0]))
                self.assertEqual(code[hi], 0x75)  # JNE RETURN; pcsp changes after LEA

    def test_direct_resume_targets(self):
        metadata = json.loads((ANTI / "symbols/symbols.json").read_text())
        exports = {s.name: s for s in self.go.dynamic_symbols}
        expected = {"agy_resume__" + row["id"] for row in enabled_hooks()}
        self.assertEqual({name for name in exports if name.startswith("agy_resume__")}, expected)
        bindings = {r.address: r for r in self.image.relocations if r.has_symbol}
        for slot, row in enumerate(enabled_hooks()):
            with self.subTest(hook=row["id"]):
                name = "agy_resume__" + row["id"]
                entry = exports["agy_go__" + row["id"]]
                prologue = bytes.fromhex(metadata["prologues"][row["symbol"]])
                self.assertEqual(entry.value, metadata["hooks"][row["symbol"]])
                hook = entry.value + metadata["skips"][row["symbol"]]
                patched = bytes(self.go.get_content_from_virtual_address(hook, len(prologue)))
                self.assertEqual(patched[:2], b"\xff\x25")
                self.assertEqual(patched[6], 0xec)  # SUB opcode survives; no marker byte
                self.assertEqual(patched[6:], prologue[6:])
                self.assertEqual(hook + 6 + struct.unpack_from("<i", patched, 2)[0],
                                 exports["agy_tramp_slots"].value + slot * 8)
                self.assertEqual(exports[name].value,
                                 entry.value + metadata["skips"][row["symbol"]] + len(prologue))
                self.assertEqual(exports[name].binding, lief.ELF.Symbol.BINDING.GLOBAL)
                self.assertEqual(exports[name].type, lief.ELF.Symbol.TYPE.FUNC)
                stub = self.stubs["agy_stub__" + row["id"]]
                code = bytes(self.image.get_content_from_virtual_address(stub.value, stub.size))
                original = 16 + struct.unpack_from("<i", code, 12)[0]
                jump = original + len(prologue)
                self.assertEqual(code[jump:jump + 2], b"\xff\x25")  # JMP *resume@GOTPCREL
                got = stub.value + jump + 6 + struct.unpack_from("<i", code, jump + 2)[0]
                self.assertEqual(bindings[got].symbol.name, name)
                self.assertEqual(bindings[got].type, lief.ELF.Relocation.TYPE.X86_64_GLOB_DAT)
                self.assertEqual(code[jump + 6], 0xc3)  # RETURN follows the one-instruction tail
                hi = self.frame_hi(row)
                self.assertEqual(hi + 2 + struct.unpack_from("b", code, hi + 1)[0], jump + 6)

    def test_nullable_handler_table(self):
        for binary in (self.obj, self.image):
            names = {s.name for s in binary.symtab_symbols}
            self.assertNotIn("agy_dispatch", names)
            self.assertFalse(any(name.startswith("agy_handler__") for name in names))
        table = self.symbols["g_agy_on"]
        self.assertEqual(table.size, len(BOOKS) * 8)
        self.assertEqual(table.section.type, lief.ELF.Section.TYPE.NOBITS)
        self.assertFalse(any(table.value <= r.address < table.value + table.size
                             for r in self.image.relocations))
        prologues = json.loads((ANTI / "symbols/symbols.json").read_text())["prologues"]
        obj_stubs = {s.name: s for s in self.obj.symtab_symbols if s.name.startswith("agy_stub__")}
        relocations = [r for r in self.obj.relocations if r.has_section and r.has_symbol
                       and r.section.name == "agy_trampolines"]
        self.assertFalse(any(r.symbol.name.startswith("agy_on_") for r in relocations))
        loads = [r for r in relocations if r.symbol.name == "g_agy_on"]
        self.assertEqual(len(loads), len(self.stubs))
        for name, stub in obj_stubs.items():
            with self.subTest(stub=name):
                matches = [r for r in loads if stub.value <= r.address < stub.value + stub.size]
                self.assertEqual(len(matches), 1)
                rel = matches[0]
                index, row = self.rows[name]
                self.assertEqual(rel.type, lief.ELF.Relocation.TYPE.X86_64_PC32)
                self.assertEqual(rel.address - stub.value, 3)
                self.assertEqual(rel.addend, index * 8 - 4)
                linked = self.stubs[name]
                code = bytes(self.image.get_content_from_virtual_address(linked.value, linked.size))
                self.assertEqual(code[:3], b"\x4c\x8b\x25")  # MOV table[row](%rip), %r12
                target = linked.value + 7 + struct.unpack_from("<i", code, 3)[0]
                self.assertEqual(target, table.value + index * 8)
                self.assertEqual(code[7:12], b"\x4d\x85\xe4\x0f\x84")  # TEST %r12; JE original
                original = 16 + struct.unpack_from("<i", code, 12)[0]
                self.assertGreater(original, self.frame_hi(row))
                prologue = bytes.fromhex(prologues[row["symbol"]])
                self.assertEqual(code[original:original + len(prologue)], prologue)
                body = code[self.layout["AGY_FRAME_LO"]:self.frame_hi(row)]
                # One qword initializes the adjacent uint32 book and action fields.
                self.assertEqual(self.layout["AGY_OFF_ACTION"], self.layout["AGY_OFF_BOOK"] + 4)
                init = b"\x48\xc7\x44\x24" + bytes([self.layout["AGY_OFF_BOOK"]])
                self.assertEqual(body.count(init + struct.pack("<I", index)), 1)
                gateway = [r for r in relocations if stub.value <= r.address < stub.value + stub.size
                           and r.symbol.name in {"agy_go__CGOCALL", "agy_go__ASMCGOCALL"}]
                self.assertEqual(len(gateway), 1)
                call = gateway[0].address - stub.value - 2
                self.assertEqual(code[call:call + 2], b"\xff\x15")
                if row["mech"] == "fullcgo":
                    self.assertEqual(gateway[0].symbol.name, "agy_go__CGOCALL")
                    # Registers only: LEA block -> RBX; MOV handler -> RAX.
                    prep = b"\x48\x8d\x5c\x24\x10\x4c\x89\xe0"
                    for store in (b"\x4c\x89\x24\x24", b"\x48\x89\x04\x24",
                                  b"\x48\x89\x44\x24\x08", b"\x48\x89\x5c\x24\x08"):
                        self.assertNotIn(store, body)
                else:
                    self.assertEqual(gateway[0].symbol.name, "agy_go__ASMCGOCALL")
                    # Stack only: MOV handler -> fn; LEA block -> RAX; MOV RAX -> arg.
                    prep = b"\x4c\x89\x24\x24\x48\x8d\x44\x24\x10\x48\x89\x44\x24\x08"
                    self.assertNotIn(b"\x4c\x89\xe0", body)  # no redundant handler -> RAX
                self.assertEqual(code[call - len(prep):call], prep)
                # ABI0 permits C to clobber X15; both PASS and RETURN need Go's zero value.
                zero_x15 = b"\x45\x0f\x57\xff"
                self.assertEqual(code[call + 6:call + 10], zero_x15)
                self.assertEqual(code.count(zero_x15), 1)
                for reg, rex, modrm in (("RAX", 0x48, 0x44), ("RBX", 0x48, 0x5c),
                        ("RCX", 0x48, 0x4c), ("RDI", 0x48, 0x7c), ("RSI", 0x48, 0x74),
                        ("R8", 0x4c, 0x44), ("R9", 0x4c, 0x4c), ("R10", 0x4c, 0x54),
                        ("R11", 0x4c, 0x5c), ("RDX", 0x48, 0x54), ("RBP", 0x48, 0x6c)):
                    offset = self.layout["AGY_OFF_" + reg]
                    self.assertEqual(code[:call].count(bytes([rex, 0x89, modrm, 0x24, offset])), 1, reg)
                    self.assertEqual(code[call + 10:].count(bytes([rex, 0x8b, modrm, 0x24, offset])), 1, reg)

    def run_version(self, extra_env=None):
        with tempfile.TemporaryDirectory(prefix="agy-stub-test-") as directory:
            capture, log = Path(directory) / "capture.jsonl", Path(directory) / "native.log"
            base = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "WIRE_"))}
            env = instrumented_env(capture=str(capture), log=str(log), base=base,
                                   extra_env={"PYTHONHOME": sys.prefix, "_AGY_SBOXSERVE": None,
                                              **(extra_env or {})})
            argv = preload_argv(str(AGY), ["--version"], env=env)
            result = subprocess.run(argv, env=env, cwd=directory, capture_output=True,
                                    text=True, timeout=30)
            diagnostics = result.stdout + result.stderr + (log.read_text() if log.exists() else "")
            self.assertEqual(result.returncode, 0, diagnostics)
            self.assertRegex(result.stdout, r"\d+\.\d+\.\d+", diagnostics)
            lines = capture.read_text().splitlines() if capture.exists() else []
            return diagnostics, collections.Counter(json.loads(line).get("kind") for line in lines)

    def test_instrumented_version(self):
        for conv_id in (False, True):
            with self.subTest(conversation_id=conv_id):
                diagnostics, kinds = self.run_version({"AGY_PROC_CONV_ID": "1" if conv_id else None})
                self.assertIn("build-id ok", diagnostics)
                self.assertIn("-> RETURN", diagnostics)
                installed = set(re.findall(r"^\[antigravity/gohook\] cgo-trampoline (\w+)$",
                                           diagnostics, re.M))
                expected = {row["id"] for row in enabled_hooks() if conv_id or row["id"] != "file_open"}
                self.assertEqual(installed, expected)
                # Generic ASMCGO and specialized FULLCGO/early-return paths run during startup.
                self.assertGreater(kinds["smoke"], 0, diagnostics)
                self.assertGreater(kinds["readlink_filter"], 0, diagnostics)

    def test_hooks_disabled_version(self):
        diagnostics, kinds = self.run_version({"AGY_PROC_ENABLE": None})
        self.assertNotIn("cgocall-trampoline: installed", diagnostics)
        self.assertNotIn("-> RETURN", diagnostics)
        self.assertFalse(kinds, diagnostics)


if __name__ == "__main__":
    unittest.main(verbosity=2)
