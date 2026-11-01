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
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

import lief

from pyagy import BOOKS, enabled_books
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
        cls.descriptors = cls.symbols["agy_stubs"]  # the constexpr table goframe.cpp defines
        cls.stubs = {name: s for name, s in cls.symbols.items() if name.startswith("agy_stub__")}
        cls.layout = {name: int(value) for name, value in re.findall(
            r"^#define\s+(AGY_\w+)\s+(\d+)\b", (ANTI / "src/goframe.h").read_text(), re.M)}
        cls.rows = {"agy_stub__" + row["id"]: (index, row) for index, row in enumerate(BOOKS)}

    def frame_hi(self, row):
        return self.pc_offset(row, "frame_end")

    def pc_offset(self, row, label):
        return (self.symbols[f"agy_pc__{row['id']}_{label}"].value
                - self.stubs["agy_stub__" + row["id"]].value)

    def test_function_and_frame_geometry(self):
        expected = {"agy_stub__" + row["id"] for row in enabled_books()}
        self.assertEqual(set(self.stubs), expected)
        self.assertIsNone(self.image.get_section("agy_trampolines"))
        self.assertFalse(any(name in self.symbols for name in
                             ("__start_agy_trampolines", "__stop_agy_trampolines")))
        for binary in (self.obj, self.image):
            self.assertFalse(any("bind_labels" in symbol.name for symbol in binary.symtab_symbols))
        descriptors = self.descriptors
        self.assertEqual(descriptors.binding, lief.ELF.Symbol.BINDING.GLOBAL)
        self.assertEqual(descriptors.visibility, lief.ELF.Symbol.VISIBILITY.HIDDEN)
        self.assertFalse(self.image.has_dynamic_symbol("agy_stubs"))
        self.assertEqual(descriptors.size, len(expected) * 32)
        raw = bytes(self.image.get_content_from_virtual_address(descriptors.value, descriptors.size))
        for row, pointers in zip(enabled_books(), struct.iter_unpack("<4Q", raw), strict=True):
            self.assertEqual(pointers, (self.stubs["agy_stub__" + row["id"]].value,
                *(self.symbols[f"agy_pc__{row['id']}_{label}"].value
                  for label in ("frame_start", "frame_end", "end"))))
        frame = struct.pack("b", self.layout["AGY_FRAME"])
        previous_end = 0
        for stub in sorted(self.stubs.values(), key=lambda s: s.value):
            with self.subTest(stub=stub.name):
                row = self.rows[stub.name][1]
                lo, hi, end = (self.pc_offset(row, label)
                               for label in ("frame_start", "frame_end", "end"))
                self.assertGreaterEqual(stub.value, previous_end)
                previous_end = stub.value + end
                self.assertTrue(0 < lo < hi < end)
                self.assertEqual(stub.size, end)
                code = bytes(self.image.get_content_from_virtual_address(stub.value, stub.size))
                self.assertEqual(code[-1], 0xc3)  # no fixed-slot padding after RETURN
                self.assertEqual(code[lo - 4:lo], b"\x48\x83\xec" + frame)
                self.assertEqual(code[hi - 5:hi], b"\x48\x8d\x64\x24" + frame)
                # CMP survives the flag-preserving stack restore for either verdict.
                action = self.layout["AGY_OFF_ACTION"]
                self.assertEqual(code[hi - 10:hi - 5], b"\x83\x7c\x24" + bytes([action, 0]))
                self.assertEqual(code[hi], 0x75)  # JNE RETURN; pcsp changes after LEA

    def test_metadata_lookup_and_validation(self):
        compiler = shutil.which("x86_64-conda-linux-gnu-c++")
        if compiler is None:
            self.skipTest("run this native fixture through pixi; the pinned C++ compiler is missing")
        with tempfile.TemporaryDirectory(prefix="agy-gomod-test-") as directory:
            executable = Path(directory) / "gomod-test"
            command = [compiler, "-std=c++23", "-O2", "-Wall", "-Wextra",
                       "-I" + str(ANTI / "src"), "-I" + str(ROOT / "wirecap/native"),
                       str(ANTI / "src/gomod.cpp"),
                       str(ROOT / "test_scripts/fixtures/agy_gomod.cpp"), "-o", str(executable)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            linked = Path(directory) / "linked-stubs.txt"
            descriptor = self.descriptors
            raw = bytes(self.image.get_content_from_virtual_address(descriptor.value, descriptor.size))
            linked.write_text("".join(" ".join(f"{p:x}" for p in pointers) + "\n"
                                      for pointers in struct.iter_unpack("<4Q", raw)))
            for case in ("linked", "sparse", "single", "frame_start", "frame_end", "end",
                         "overlap", "module_overlap", "count", "size"):
                with self.subTest(case=case):
                    args = [str(executable), case] + ([str(linked)] if case == "linked" else [])
                    result = subprocess.run(args, capture_output=True,
                                            text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_direct_resume_targets(self):
        metadata = json.loads((ANTI / "symbols/symbols.json").read_text())
        exports = {s.name: s for s in self.go.dynamic_symbols}
        expected = {"agy_resume__" + row["id"] for row in enabled_books()}
        self.assertEqual({name for name in exports if name.startswith("agy_resume__")}, expected)
        bindings = {r.address: r for r in self.image.relocations if r.has_symbol}
        for row in enabled_books():
            with self.subTest(book=row["id"]):
                name = "agy_resume__" + row["id"]
                entry = exports["agy_go__" + row["id"]]
                prologue = bytes.fromhex(metadata["prologues"][row["symbol"]])
                self.assertEqual(entry.value, metadata["books"][row["symbol"]])
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

    def test_plt_book_bindings(self):
        rows = enabled_books()
        metadata = json.loads((ANTI / "symbols/symbols.json").read_text())
        self.assertFalse(self.go.has_symbol("agy_tramp_slots"))
        plt, got = (self.go.get_section(name) for name in (".plt.agy", ".got.plt.agy"))
        for section, permissions in ((plt, 5), (got, 6)):  # ELF PF_R|PF_X / PF_R|PF_W
            self.assertIsNotNone(section)
            self.assertGreaterEqual(section.size, len(rows) * 8)  # LIEF may add page padding
            self.assertEqual(section.virtual_address % 8, 0)
            segments = [s for s in self.go.segments if s.type == lief.ELF.Segment.TYPE.LOAD
                        and s.virtual_address <= section.virtual_address
                        and section.virtual_address + section.size <= s.virtual_address + s.virtual_size]
            self.assertTrue(segments)
            for segment in segments:
                self.assertEqual(int(segment.flags) & 7, permissions)
        tag = lief.ELF.DynamicEntry.TAG
        flags, flags1 = self.go.get(tag.FLAGS), self.go.get(tag.FLAGS_1)
        self.assertTrue(self.go.has(tag.BIND_NOW) or (flags and flags.value & 8)
                        or (flags1 and flags1.value & 1))
        self.assertEqual(self.go.get(tag.PLTREL).value, 7)  # DT_RELA
        reloc_data = bytes(self.go.get_content_from_virtual_address(
            self.go.get(tag.JMPREL).value, self.go.get(tag.PLTRELSZ).value))
        records = {address: (info, addend) for address, info, addend in struct.iter_unpack("<QQq", reloc_data)}
        symbols = list(self.go.dynamic_symbols)
        relocations = {r.address: r for r in self.go.pltgot_relocations}
        imports = [r for r in self.go.relocations if r.has_symbol
                   and r.symbol.name.startswith("agy_stub__")]
        self.assertEqual(len(imports), len(rows))
        for index, row in enumerate(rows):
            with self.subTest(book=row["id"]):
                book = metadata["books"][row["symbol"]] + metadata["skips"][row["symbol"]]
                prologue = bytes.fromhex(metadata["prologues"][row["symbol"]])
                patched = bytes(self.go.get_content_from_virtual_address(book, len(prologue)))
                relay, slot = plt.virtual_address + index * 8, got.virtual_address + index * 8
                self.assertEqual(patched[0], 0xe9)
                self.assertEqual(book + 5 + struct.unpack_from("<i", patched, 1)[0], relay)
                self.assertEqual(patched[5:], b"\xcc" * (len(prologue) - 5))  # unreachable tail
                code = bytes(self.go.get_content_from_virtual_address(relay, 8))
                self.assertEqual(code[:2], b"\xff\x25")
                self.assertEqual(relay + 6 + struct.unpack_from("<i", code, 2)[0], slot)
                self.assertEqual(code[6:], b"\x66\x90")
                expected = "agy_stub__" + row["id"]
                self.assertEqual(relocations[slot].symbol.name, expected)
                self.assertEqual(relocations[slot].type, lief.ELF.Relocation.TYPE.X86_64_JUMP_SLOT)
                info, addend = records[slot]  # independently read the actual DT_JMPREL table
                self.assertEqual(info & 0xffffffff, 7)  # R_X86_64_JUMP_SLOT
                self.assertEqual(symbols[info >> 32].name, expected)
                self.assertEqual(addend, 0)

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
        stub_sections = {s.section.name for s in obj_stubs.values()}
        relocations = [r for r in self.obj.relocations if r.has_section and r.has_symbol
                       and r.section.name in stub_sections]
        self.assertFalse(any(r.symbol.name.startswith("agy_on_") for r in relocations))
        loads = [r for r in relocations if r.symbol.name == "g_agy_on"]
        self.assertEqual(len(loads), len(self.stubs))
        for name, stub in obj_stubs.items():
            with self.subTest(stub=name):
                in_stub = lambda r: (r.section.name == stub.section.name
                                     and stub.value <= r.address < stub.value + stub.size)
                matches = [r for r in loads if in_stub(r)]
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
                body = code[self.pc_offset(row, "frame_start"):self.frame_hi(row)]
                # One qword initializes the adjacent uint32 book and action fields.
                self.assertEqual(self.layout["AGY_OFF_ACTION"], self.layout["AGY_OFF_BOOK"] + 4)
                init = b"\x48\xc7\x44\x24" + bytes([self.layout["AGY_OFF_BOOK"]])
                self.assertEqual(body.count(init + struct.pack("<I", index)), 1)
                gateway = [r for r in relocations if in_stub(r)
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
            self.assertEqual(result.returncode, 0, diagnostics[-8000:])
            self.assertRegex(result.stdout, r"\d+\.\d+\.\d+", diagnostics[-8000:])
            lines = capture.read_text().splitlines() if capture.exists() else []
            return diagnostics, collections.Counter(json.loads(line).get("kind") for line in lines)

    def test_instrumented_version(self):
        for conv_id in (False, True):
            with self.subTest(conversation_id=conv_id):
                diagnostics, kinds = self.run_version({"AGY_PROC_CONV_ID": "1" if conv_id else None})
                self.assertIn("build-id ok", diagnostics)
                self.assertIn("-> RETURN", diagnostics)
                installed = set(re.findall(r"^\[antigravity/gobook\] cgo-trampoline (\w+)$",
                                           diagnostics, re.M))
                expected = {row["id"] for row in enabled_books() if conv_id or row["id"] != "file_open"}
                self.assertEqual(installed, expected)
                # Generic ASMCGO and specialized FULLCGO/early-return paths run during startup.
                self.assertGreater(kinds["smoke"], 0, diagnostics)
                self.assertGreater(kinds["readlink_filter"], 0, diagnostics)

    def test_books_disabled_version(self):
        diagnostics, kinds = self.run_version({"AGY_PROC_ENABLE": None})
        self.assertNotIn("cgocall-trampoline: installed", diagnostics)
        self.assertNotIn("-> RETURN", diagnostics)
        self.assertFalse(kinds, diagnostics)

    def test_plt_resolved_before_go_start(self):
        diagnostics, kinds = self.run_version({"AGY_PROC_ENABLE": None, "LD_BIND_NOW": None,
                                              "LD_DEBUG": "bindings", "LD_DEBUG_OUTPUT": None})
        marker = "transferring control: " + str(AGY)
        self.assertTrue(marker in diagnostics, diagnostics[-8000:])
        before_main = diagnostics[:diagnostics.index(marker)]
        bound = set(re.findall(r"binding file [^\n]*libagy\.so \[\d+\] to [^\n]*"
                               r"symbol [`'](agy_stub__\w+)['`]", before_main))
        self.assertEqual(bound, set(self.stubs))
        self.assertFalse(kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
