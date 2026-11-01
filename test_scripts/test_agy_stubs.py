#!/usr/bin/env python3
"""Offline native checks for the built agy stubs; no provider calls.

Run after ``pixi run build-shim`` with ``pixi run python test_scripts/test_agy_stubs.py``.
Missing build artifacts produce an explicit skip, not a passing native check.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import cast

import lief
from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from capstone.x86_const import X86_INS_JMP, X86_OP_IMM, X86_OP_MEM, X86_REG_RIP
from elftools.elf.dynamic import DynamicSection
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import Relocation, RelocationSection
from elftools.elf.sections import Symbol, SymbolTableSection
from pyagy import BOOKS, enabled_books
from pyagy._env import linked_argv, linked_env

from wirecap.types import json_loads_object

ROOT = Path(__file__).resolve().parents[1]
ANTI = ROOT / "antigravity"
AGY = ANTI / "vendor/agy"
OBJECT = ANTI / "build/CMakeFiles/agy.dir/src/goframe.cpp.o"


sys.path.insert(0, str(ANTI))
from symbols.agyelf import BookTarget, SymbolsMetadata


def section_of(item: lief.ELF.Symbol | lief.ELF.Relocation) -> lief.ELF.Section:
    section = item.section
    assert section is not None
    return section


def symbol_of(relocation: lief.ELF.Relocation) -> lief.ELF.Symbol:
    symbol = relocation.symbol
    assert symbol is not None
    return symbol


def unpack_int(format: str, data: bytes, offset: int) -> int:
    return cast(tuple[int], struct.unpack_from(format, data, offset))[0]


def native_name(item: lief.ELF.Symbol | lief.ELF.Section) -> str:
    name = item.name
    assert isinstance(name, str)
    return name


parse_elf = cast(Callable[[str], lief.ELF.Binary | None], lief.ELF.parse)


class AgyStubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        missing = [p for p in (AGY, OBJECT, ANTI / "vendor/libagy.so", ANTI / "symbols/symbols.json")
                   if not p.is_file()]
        if missing:
            raise unittest.SkipTest(f"run pixi run build-shim; missing: {missing}")
        lief.logging.disable()
        image = parse_elf(str(AGY))
        obj = parse_elf(str(OBJECT))
        assert image is not None and obj is not None
        cls.image, cls.obj = image, obj
        cls.go_bytes = (ANTI / "vendor/libagy.so").read_bytes()
        cls.go_elf = ELFFile(BytesIO(cls.go_bytes))
        dynsym = cls.go_elf.get_section_by_name(".dynsym")
        assert isinstance(dynsym, SymbolTableSection)
        cls.go_symbols = list(dynsym.iter_symbols())
        cls.go_exports = {s.name: s for s in cls.go_symbols}
        cls.metadata = cast(SymbolsMetadata, json_loads_object((ANTI / "symbols/symbols.json").read_text()))
        cls.symbols = {native_name(s): s for s in cls.image.symtab_symbols}
        cls.descriptors = cls.symbols["agy_stubs"]  # the constexpr table goframe.cpp defines
        cls.stubs = {name: s for name, s in cls.symbols.items() if name.startswith("agy_stub__")}
        cls.layout = {match.group(1): int(match.group(2)) for match in re.finditer(
            r"^#define\s+(AGY_\w+)\s+(\d+)\b", (ANTI / "src/goframe.h").read_text(), re.M)}
        cls.rows = {"agy_stub__" + row["id"]: (index, row) for index, row in enumerate(BOOKS)}

    def frame_hi(self, row: BookTarget) -> int:
        return self.pc_offset(row, "frame_end")

    def pc_offset(self, row: BookTarget, label: str) -> int:
        return (self.symbols[f"agy_pc__{row['id']}_{label}"].value
                - self.stubs["agy_stub__" + row["id"]].value)

    def host_symbol_bytes(self, symbol: lief.ELF.Symbol):
        return bytes(self.image.get_content_from_virtual_address(symbol.value, symbol.size))

    def test_function_and_frame_geometry(self) -> None:
        expected = {"agy_stub__" + row["id"] for row in enabled_books()}
        self.assertEqual(set(self.stubs), expected)
        self.assertIsNone(self.image.get_section("agy_trampolines"))
        self.assertFalse(any(name in self.symbols for name in
                             ("__start_agy_trampolines", "__stop_agy_trampolines")))
        for binary in (self.obj, self.image):
            self.assertFalse(any("bind_labels" in native_name(symbol) for symbol in binary.symtab_symbols))
        descriptors = self.descriptors
        self.assertEqual(descriptors.binding, lief.ELF.Symbol.BINDING.GLOBAL)
        self.assertEqual(descriptors.visibility, lief.ELF.Symbol.VISIBILITY.HIDDEN)
        self.assertFalse(self.image.has_dynamic_symbol("agy_stubs"))
        self.assertEqual(descriptors.size, len(expected) * 48)
        raw = self.host_symbol_bytes(descriptors)
        for row, pointers in zip(enabled_books(), struct.iter_unpack("<4QIIQ", raw), strict=True):
            self.assertEqual(pointers[:4], (self.stubs["agy_stub__" + row["id"]].value,
                *(self.symbols[f"agy_pc__{row['id']}_{label}"].value
                  for label in ("frame_start", "frame_end", "end"))))
            gc_map = self.metadata["gc_maps"][row["symbol"]]
            self.assertEqual(pointers[4:], (gc_map["reg_ptrmask"], gc_map["stack_args_size"],
                                            gc_map["stack_ptrmask"]))
        frame = struct.pack("b", self.layout["AGY_FRAME"])
        previous_end = 0
        for stub in sorted(self.stubs.values(), key=lambda s: s.value):
            with self.subTest(stub=stub.name):
                row = self.rows[native_name(stub)][1]
                lo, hi, end = (self.pc_offset(row, label)
                               for label in ("frame_start", "frame_end", "end"))
                self.assertGreaterEqual(stub.value, previous_end)
                previous_end = stub.value + end
                self.assertTrue(0 < lo < hi < end)
                self.assertEqual(stub.size, end)
                code = self.host_symbol_bytes(stub)
                self.assertEqual(code[-1], 0xc3)  # no fixed-slot padding after RETURN
                self.assertEqual(code[lo - 4:lo], b"\x48\x83\xec" + frame)
                self.assertEqual(code[hi - 5:hi], b"\x48\x8d\x64\x24" + frame)
                # CMP survives the flag-preserving stack restore for either verdict.
                action = self.layout["AGY_OFF_ACTION"]
                self.assertEqual(code[hi - 10:hi - 5], b"\x83\x7c\x24" + bytes([action, 0]))
                self.assertEqual(code[hi], 0x75)  # JNE RETURN; pcsp changes after LEA

    def test_metadata_lookup_and_validation(self) -> None:
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
            raw = self.host_symbol_bytes(self.descriptors)
            linked.write_text("".join(" ".join(f"{p:x}" for p in pointers) + "\n"
                                      for pointers in struct.iter_unpack("<4QIIQ", raw)))
            for case in ("linked", "sparse", "single", "frame_start", "frame_end", "end",
                         "overlap", "module_overlap", "count", "size", "serializer", "map_capacity",
                         "bad_regs", "bad_args_alignment", "bad_args_size", "bad_args_mask", "empty_args_mask"):
                with self.subTest(case=case):
                    args = [str(executable), case] + ([str(linked)] if case == "linked" else [])
                    result = subprocess.run(args, capture_output=True,
                                            text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_direct_resume_targets(self) -> None:
        metadata, exports = self.metadata, self.go_exports
        expected = {"agy_resume__" + row["id"] for row in enabled_books()}
        self.assertEqual({name for name in exports if name.startswith("agy_resume__")}, expected)
        bindings = {r.address: r for r in self.image.relocations if r.has_symbol}
        for row in enabled_books():
            with self.subTest(book=row["id"]):
                name = "agy_resume__" + row["id"]
                entry = exports["agy_go__" + row["id"]]
                prologue = bytes.fromhex(metadata["prologues"][row["symbol"]])
                self.assertEqual(entry["st_value"], metadata["books"][row["symbol"]])
                self.assertEqual(exports[name]["st_value"],
                                 entry["st_value"] + metadata["skips"][row["symbol"]] + len(prologue))
                self.assertEqual(exports[name]["st_info"]["bind"], "STB_GLOBAL")
                self.assertEqual(exports[name]["st_info"]["type"], "STT_FUNC")
                stub = self.stubs["agy_stub__" + row["id"]]
                code = self.host_symbol_bytes(stub)
                original = 16 + unpack_int("<i", code, 12)
                jump = original + len(prologue)
                self.assertEqual(code[jump:jump + 2], b"\xff\x25")  # JMP *resume@GOTPCREL
                got = stub.value + jump + 6 + unpack_int("<i", code, jump + 2)
                self.assertEqual(symbol_of(bindings[got]).name, name)
                self.assertEqual(bindings[got].type, lief.ELF.Relocation.TYPE.X86_64_GLOB_DAT)
                self.assertEqual(code[jump + 6], 0xc3)  # RETURN follows the one-instruction tail
                hi = self.frame_hi(row)
                self.assertEqual(hi + 2 + unpack_int("b", code, hi + 1), jump + 6)

    def test_plt_book_bindings(self) -> None:
        # Check LIEF's output with pyelftools and pwntools' jumps with Capstone.
        elf = self.go_elf
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        decoder.detail = True

        def read(address: int, size: int):
            offsets = list(elf.address_offsets(address, size))
            self.assertEqual(len(offsets), 1)
            return self.go_bytes[offsets[0]:offsets[0] + size]

        rows = enabled_books()
        metadata = self.metadata
        self.assertNotIn("agy_tramp_slots", self.go_exports)
        plt, got = (elf.get_section_by_name(name) for name in (".plt.agy", ".got.plt.agy"))
        assert plt is not None and got is not None
        for section, permissions in ((plt, 5), (got, 6)):  # ELF PF_R|PF_X / PF_R|PF_W
            assert section is not None
            self.assertGreaterEqual(section["sh_size"], len(rows) * 8)  # writer may add page padding
            self.assertEqual(section["sh_addr"] % 8, 0)
            segments = [s for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"
                        and s.section_in_segment(section)]
            self.assertTrue(segments)
            for segment in segments:
                self.assertEqual(segment["p_flags"] & 7, permissions)
        dynamic = elf.get_section_by_name(".dynamic")
        assert isinstance(dynamic, DynamicSection)
        tags = {tag.entry.d_tag: tag.entry.d_val for tag in dynamic.iter_tags()}
        self.assertTrue("DT_BIND_NOW" in tags or tags.get("DT_FLAGS", 0) & 8
                        or tags.get("DT_FLAGS_1", 0) & 1)
        self.assertEqual(tags["DT_PLTREL"], 7)  # DT_RELA
        reloc_data = read(tags["DT_JMPREL"], tags["DT_PLTRELSZ"])
        records = {address: (info, addend) for address, info, addend in struct.iter_unpack("<QQq", reloc_data)}
        imports: dict[int, tuple[Relocation, Symbol]] = {}
        for section in elf.iter_sections():
            if not isinstance(section, RelocationSection):
                continue
            table = elf.get_section(section["sh_link"])
            assert isinstance(table, SymbolTableSection)
            for rel in section.iter_relocations():
                if not rel["r_info_sym"]:
                    continue
                symbol = table.get_symbol(rel["r_info_sym"])
                if symbol.name.startswith("agy_stub__"):
                    self.assertNotIn(rel["r_offset"], imports)
                    imports[rel["r_offset"]] = (rel, symbol)
        self.assertEqual(len(imports), len(rows))
        for index, row in enumerate(rows):
            with self.subTest(book=row["id"]):
                book = metadata["books"][row["symbol"]] + metadata["skips"][row["symbol"]]
                prologue = bytes.fromhex(metadata["prologues"][row["symbol"]])
                patched = read(book, len(prologue))
                relay, slot = plt["sh_addr"] + index * 8, got["sh_addr"] + index * 8
                jump = next(decoder.disasm(patched, book, count=1))
                self.assertEqual((jump.id, jump.size), (X86_INS_JMP, 5))
                self.assertEqual(jump.operands[0].type, X86_OP_IMM)
                self.assertEqual(jump.operands[0].imm, relay)
                self.assertEqual(patched[5:], b"\xcc" * (len(prologue) - 5))  # unreachable tail
                code = read(relay, 8)
                indirect = next(decoder.disasm(code, relay, count=1))
                self.assertEqual((indirect.id, indirect.size), (X86_INS_JMP, 6))
                self.assertEqual(indirect.operands[0].type, X86_OP_MEM)
                memory = indirect.operands[0].mem
                self.assertEqual(memory.base, X86_REG_RIP)
                self.assertEqual(memory.index, 0)
                self.assertEqual(indirect.address + indirect.size + memory.disp, slot)
                self.assertEqual(code[6:], b"\x66\x90")
                expected = "agy_stub__" + row["id"]
                relocation, symbol = imports[slot]
                self.assertEqual(symbol.name, expected)
                self.assertEqual(symbol["st_shndx"], "SHN_UNDEF")
                self.assertEqual(symbol["st_info"]["bind"], "STB_GLOBAL")
                self.assertEqual(relocation["r_info_type"], 7)  # R_X86_64_JUMP_SLOT
                info, addend = records[slot]  # independently read the actual DT_JMPREL table
                self.assertEqual(info & 0xffffffff, 7)  # R_X86_64_JUMP_SLOT
                self.assertEqual(self.go_symbols[info >> 32].name, expected)
                self.assertEqual(addend, 0)

    def test_nullable_handler_table(self) -> None:
        for binary in (self.obj, self.image):
            names = {native_name(s) for s in binary.symtab_symbols}
            self.assertNotIn("agy_dispatch", names)
            self.assertFalse(any(name.startswith("agy_handler__") for name in names))
        table = self.symbols["g_agy_on"]
        self.assertEqual(table.size, len(BOOKS) * 8)
        self.assertEqual(section_of(table).type, lief.ELF.Section.TYPE.NOBITS)
        self.assertFalse(any(table.value <= r.address < table.value + table.size
                             for r in self.image.relocations))
        prologues = self.metadata["prologues"]
        obj_stubs = {native_name(s): s for s in self.obj.symtab_symbols
                     if native_name(s).startswith("agy_stub__")}
        stub_sections = {section_of(s).name for s in obj_stubs.values()}
        relocations = [r for r in self.obj.relocations if r.has_section and r.has_symbol
                       and section_of(r).name in stub_sections]
        self.assertFalse(any(native_name(symbol_of(r)).startswith("agy_on_") for r in relocations))
        loads = [r for r in relocations if symbol_of(r).name == "g_agy_on"]
        self.assertEqual(len(loads), len(self.stubs))
        for name, stub in obj_stubs.items():
            with self.subTest(stub=name):
                def in_stub(relocation: lief.ELF.Relocation) -> bool:
                    return (section_of(relocation).name == section_of(stub).name
                            and stub.value <= relocation.address < stub.value + stub.size)
                matches = [r for r in loads if in_stub(r)]
                self.assertEqual(len(matches), 1)
                rel = matches[0]
                index, row = self.rows[name]
                self.assertEqual(rel.type, lief.ELF.Relocation.TYPE.X86_64_PC32)
                self.assertEqual(rel.address - stub.value, 3)
                self.assertEqual(rel.addend, index * 8 - 4)
                linked = self.stubs[name]
                code = self.host_symbol_bytes(linked)
                self.assertEqual(code[:3], b"\x4c\x8b\x25")  # MOV table[row](%rip), %r12
                target = linked.value + 7 + unpack_int("<i", code, 3)
                self.assertEqual(target, table.value + index * 8)
                self.assertEqual(code[7:12], b"\x4d\x85\xe4\x0f\x84")  # TEST %r12; JE original
                original = 16 + unpack_int("<i", code, 12)
                self.assertGreater(original, self.frame_hi(row))
                prologue = bytes.fromhex(prologues[row["symbol"]])
                self.assertEqual(code[original:original + len(prologue)], prologue)
                body = code[self.pc_offset(row, "frame_start"):self.frame_hi(row)]
                # One qword initializes the adjacent uint32 book and action fields.
                self.assertEqual(self.layout["AGY_OFF_ACTION"], self.layout["AGY_OFF_BOOK"] + 4)
                init = b"\x48\xc7\x44\x24" + bytes([self.layout["AGY_OFF_BOOK"]])
                self.assertEqual(body.count(init + struct.pack("<I", index)), 1)
                gateway = [r for r in relocations if in_stub(r)
                           and symbol_of(r).name in {"agy_go__CGOCALL", "agy_go__ASMCGOCALL"}]
                self.assertEqual(len(gateway), 1)
                call = gateway[0].address - stub.value - 2
                self.assertEqual(code[call:call + 2], b"\xff\x15")
                if row["mech"] == "fullcgo":
                    self.assertEqual(symbol_of(gateway[0]).name, "agy_go__CGOCALL")
                    # Registers only: LEA block -> RBX; MOV handler -> RAX.
                    prep = b"\x48\x8d\x5c\x24\x10\x4c\x89\xe0"
                    for store in (b"\x4c\x89\x24\x24", b"\x48\x89\x04\x24",
                                  b"\x48\x89\x44\x24\x08", b"\x48\x89\x5c\x24\x08"):
                        self.assertNotIn(store, body)
                else:
                    self.assertEqual(symbol_of(gateway[0]).name, "agy_go__ASMCGOCALL")
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

    def run_version(self, extra_env: dict[str, str | None] | None=None):
        with tempfile.TemporaryDirectory(prefix="agy-stub-test-") as directory:
            capture, log = Path(directory) / "capture.jsonl", Path(directory) / "native.log"
            base = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "WIRE_"))}
            env = linked_env(capture=str(capture), log=str(log), base=base,
                                   extra_env={"PYTHONHOME": sys.prefix, "_AGY_SBOXSERVE": None,
                                              **(extra_env or {})})
            argv = linked_argv(str(AGY), ["--version"], env=env)
            result = subprocess.run(argv, env=env, cwd=directory, capture_output=True,
                                    text=True, timeout=30)
            diagnostics = result.stdout + result.stderr + (log.read_text() if log.exists() else "")
            self.assertEqual(result.returncode, 0, diagnostics[-8000:])
            self.assertRegex(result.stdout, r"\d+\.\d+\.\d+", diagnostics[-8000:])
            lines = capture.read_text().splitlines() if capture.exists() else []
            kinds: collections.Counter[str] = collections.Counter()
            for line in lines:
                kind = json_loads_object(line)["kind"]
                assert isinstance(kind, str)
                kinds[kind] += 1
            return diagnostics, kinds

    def test_linked_version(self) -> None:
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

    def test_gc_stress_version(self) -> None:
        diagnostics, kinds = self.run_version({"GOGC": "1",
            "GODEBUG": "invalidptr=1,clobberfree=1,gccheckmark=1,gctrace=1"})
        self.assertRegex(diagnostics, r"gc [0-9]+ @")
        self.assertIn("precise pointer maps", diagnostics)
        self.assertGreater(kinds["readlink_filter"], 0)

    def test_books_disabled_version(self) -> None:
        diagnostics, kinds = self.run_version({"AGY_PROC_ENABLE": None})
        self.assertNotIn("cgocall-trampoline: installed", diagnostics)
        self.assertNotIn("-> RETURN", diagnostics)
        self.assertFalse(kinds, diagnostics)

    def test_plt_resolved_before_go_start(self) -> None:
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
