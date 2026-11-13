"""Offline checks for agy's symbol tools and ELF conversion, using synthetic images.

Run with ``pixi run python test_scripts/test_agy_symbols.py``.
"""

from __future__ import annotations

import gzip
import json
import struct
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from elftools.elf.enums import ENUM_D_TAG

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "antigravity"))

from symbols import build_symbols, gen_sources, make_libagy  # noqa: E402
from symbols.agyelf import (  # noqa: E402
    ELF,
    Book,
    BookTarget,
    EntryGCMap,
    GoImage,
    PatchMetadata,
    active_rows,
    derive_entry_gc_map,
    frame_prologue_hex,
    load_procdef,
    parse_procdef,
    prologue_skip,
)
from symbols.gen_sources import BOOKS_OUT, render_books  # noqa: E402
from symbols.x86 import RegisterSpill, morestack_spills, planner_response_itab  # noqa: E402

from wirecap.types import json_loads_object


class ProcdefTests(unittest.TestCase):
    def test_wrappers_and_symbol_literals(self) -> None:
        rows = parse_procdef('''
#define CAC "example/pkg.(*Client)."
{ "resp", WIRE_ASYNC, 0, AGY_FULLGO(CAC "Read(func())") },
{ "smoke", WIRE_SYNC, 0, AGY_ASMGO("os.Getenv") },
{ "special", WIRE_ASYNC, -1, AGY_OFF("crypto/tls.(*Conn).Read") },
{ "buffer", WIRE_ASYNC, 256, AGY_OFF("example.Buffer") },
''')
        self.assertEqual(rows, [
            {"id": "resp", "symbol": "example/pkg.(*Client).Read(func())",
             "mode": "async", "mech": "fullcgo", "retcap": 0},
            {"id": "smoke", "symbol": "os.Getenv",
             "mode": "sync", "mech": "asmcgo", "retcap": 0},
            {"id": "special", "symbol": "crypto/tls.(*Conn).Read",
             "mode": "async", "mech": "off", "retcap": -1},
            {"id": "buffer", "symbol": "example.Buffer",
             "mode": "async", "mech": "off", "retcap": 256},
        ])
        self.assertEqual([row["id"] for row in active_rows(rows)], ["resp", "smoke"])

    def test_checked_in_python_mirror(self) -> None:
        self.assertEqual(render_books(load_procdef()), Path(BOOKS_OUT).read_text())


class ELFReaderTests(unittest.TestCase):
    @staticmethod
    def image(machine: int=62, elfclass: int=2, endian: int=1):
        # A writable PT_LOAD with 256 file bytes followed by 256 bytes of BSS.
        ident = b"\x7fELF" + bytes((elfclass, endian, 1)) + bytes(9)
        header = struct.pack("<16sHHIQQQIHHHHHH", ident, 3, machine, 1,
                             0, 64, 0, 0, 64, 56, 1, 64, 0, 0)
        segment = struct.pack("<IIQQQQQQ", 1, 6, 0, 0x1000, 0x1000,
                              256, 512, 0x1000)
        return (header + segment).ljust(256, b"\0")

    def test_file_backed_ranges_exclude_bss_and_crossing_reads(self) -> None:
        elf = ELF(self.image())
        self.assertEqual(elf.v2o(0x1000, 256), 0)
        self.assertEqual(elf.v2o(0x10ff), 255)
        for address, size in ((0xfff, 1), (0x10ff, 2), (0x1100, 1), (0x11ff, 1)):
            with self.subTest(address=address, size=size), self.assertRaises(ValueError):
                elf.v2o(address, size)

    def test_rejects_other_elf_abis(self) -> None:
        for kwargs in ({"machine": 183}, {"elfclass": 1}, {"endian": 2}):
            with self.subTest(**kwargs), self.assertRaises(SystemExit):
                ELF(self.image(**kwargs))

    @staticmethod
    def dynamic_image(overrides: dict[str, int] | None=None):
        """A sectionless ELF with distinct runtime PLT and ordinary relocation tables."""
        base, size, dynamic_offset = 0x1000, 1024, 192
        symbols_offset, strings_offset = 416, 496
        plt_offset, rela_offset, got_offset = 560, 584, 640
        strings = b"\0agy_stub__fixture\0agy_stub__decoy\0"
        tags = {"DT_PLTRELSZ": 24, "DT_PLTGOT": base + got_offset,
                "DT_STRTAB": base + strings_offset, "DT_SYMTAB": base + symbols_offset,
                "DT_STRSZ": len(strings), "DT_SYMENT": 24,
                "DT_PLTREL": ENUM_D_TAG["DT_RELA"], "DT_JMPREL": base + plt_offset,
                "DT_RELA": base + rela_offset, "DT_RELASZ": 24, "DT_RELAENT": 24,
                "DT_FLAGS": 8}  # DF_BIND_NOW
        tags.update(overrides or {})
        dynamic = b"".join(struct.pack("<qQ", ENUM_D_TAG[tag], value)
                           for tag, value in tags.items()) + bytes(16)
        ident = b"\x7fELF\x02\x01\x01" + bytes(9)
        header = struct.pack("<16sHHIQQQIHHHHHH", ident, 3, 62, 1,
                             0, 64, 0, 0, 64, 56, 2, 64, 0, 0)
        load = struct.pack("<IIQQQQQQ", 1, 6, 0, base, base, size, size + 128, 0x1000)
        segment = struct.pack("<IIQQQQQQ", 2, 6, dynamic_offset, base + dynamic_offset,
                              base + dynamic_offset, len(dynamic), len(dynamic), 8)
        data = bytearray((header + load + segment).ljust(size, b"\0"))
        data[dynamic_offset:dynamic_offset + len(dynamic)] = dynamic
        data[strings_offset:strings_offset + len(strings)] = strings
        for index, name_offset in ((1, 1), (2, 19)):
            struct.pack_into("<IBBHQQ", data, symbols_offset + index * 24,
                             name_offset, 0x12, 0, 0, 0, 0)
        for offset, index in ((plt_offset, 1), (rela_offset, 2)):
            struct.pack_into("<QQq", data, offset, base + got_offset, (index << 32) | 7, 0)
        return bytes(data)

    def test_plt_relocations_follow_dynamic_table_without_sections(self) -> None:
        for table, expected in ((560, "agy_stub__fixture"), (584, "agy_stub__decoy")):
            with self.subTest(table=table):
                elf = ELF(self.dynamic_image({"DT_JMPREL": 0x1000 + table}))
                self.assertEqual(elf.elf.num_sections(), 0)
                relocations = list(elf.plt_relocations())
                self.assertEqual(len(relocations), 1)
                self.assertEqual(tuple(relocations[0]), (0x1280, 7, expected, 0))

    def test_plt_relocations_reject_bad_format_or_file_range(self) -> None:
        cases = (
            {"DT_PLTREL": ENUM_D_TAG["DT_REL"]},  # unsupported AMD64 PLT format
            {"DT_PLTRELSZ": 23},                 # partial Elf64_Rela record
            {"DT_JMPREL": 0x1400},               # starts in BSS
            {"DT_JMPREL": 0x13f0},               # crosses the file-backed boundary
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises((SystemExit, ValueError)):
                list(ELF(self.dynamic_image(overrides)).plt_relocations())


@dataclass
class CodeImage:
    loads: list[tuple[int, int, int]]


def code_image(data: bytes) -> CodeImage:
    """Instruction bytes mapped at address zero, without ELF headers."""
    return CodeImage(loads=[(0, 0, len(data))])


class GoPrologueTests(unittest.TestCase):
    FRAME = bytes.fromhex("554889e54883ec28")

    def test_small_and_large_stack_checks(self) -> None:
        guards = (
            "493b66107620",                         # CMP RSP, [R14+16]; JBE rel8
            "493b66100f8620000000",                 # JBE rel32
            "4c8da42478feffff4d3b66100f8620000000", # LEA R12, [RSP-392]; CMP; JBE
            "4989e44981ec801000000f82f00000004d3b66100f86e6000000", # huge: SUB/JB and CMP/JBE
        )
        for guard in guards:
            with self.subTest(guard=guard):
                prefix = bytes.fromhex(guard)
                data = prefix + self.FRAME + b"\xc3"
                elf = code_image(data)
                self.assertEqual(prologue_skip(elf, data, 0), len(prefix))
                self.assertEqual(frame_prologue_hex(elf, data, 0, len(prefix)),
                                 self.FRAME.hex())

    def test_rejects_false_or_incomplete_stack_checks(self) -> None:
        cases = {
            "embedded in MOVABS immediate": "48b8493b66107620aaaa",
            "missing branch": "493b6610",
            "wrong branch condition": "493b66107720",
            "branch into frame": "493b66107601",
            "backward branch": "493b661076f0",
            "wrong guard base": "493b65107620",
            "wrong compared register": "493b46107620",
            "uninitialized scratch register": "4d3b66107620",
            "unrelated leading call": "e800000000493b66107620",
            "huge checks disagree": "4989e44981ec801000000f82f10000004d3b66100f86e6000000",
        }
        for reason, prefix in cases.items():
            with self.subTest(reason=reason):
                data = bytes.fromhex(prefix) + self.FRAME + b"\xc3"
                self.assertEqual(prologue_skip(code_image(data), data, 0), 0)
        for data in (bytes.fromhex("493b66"), bytes.fromhex("493b66100f86"),
                     bytes.fromhex("493b661076")):
            with self.subTest(truncated=data.hex()):
                self.assertEqual(prologue_skip(code_image(data), data, 0), 0)

    def test_truncated_frame_is_not_patchable(self) -> None:
        prefix = bytes.fromhex("493b66107620")
        complete = prefix + self.FRAME
        for size in range(len(self.FRAME)):
            with self.subTest(size=size):
                data = prefix + self.FRAME[:size]
                # The frame must fit both the file and its mapped segment.
                for elf, content in ((code_image(complete), data), (code_image(data), complete)):
                    self.assertEqual(frame_prologue_hex(elf, content, 0, len(prefix)), "")
                    self.assertEqual(prologue_skip(elf, content, 0), 0)


def morestack_code(stores: bytes, loads: bytes, *, address: int = 0x1320,
                   entry: int = 0x1300, target: int = 0x2000) -> bytes:
    call = b"\xe8" + struct.pack("<i", target - (address + len(stores) + 5))
    prefix = stores + call + loads
    return prefix + b"\xe9" + struct.pack("<i", entry - (address + len(prefix) + 5))


class EntryGCMapTests(unittest.TestCase):
    STORES = bytes.fromhex("488944240848895c2410")
    LOADS = bytes.fromhex("488b442408488b5c2410")

    def test_system_serializer_stack_roots_are_separated_from_spills(self) -> None:
        stores = bytes.fromhex("48898424d000000048899c24d8000000")
        loads = bytes.fromhex("488b8424d0000000488b9c24d8000000")
        spills = morestack_spills(morestack_code(stores, loads), 0x1320, 0x1300, 0x2000)
        roots = derive_entry_gc_map(216, 26, 0x02aa801a, spills)
        self.assertEqual(roots, {"reg_ptrmask": 1, "stack_args_size": 200,
                                "stack_ptrmask": 0xaa801a, "args_size": 216,
                                "args_nbit": 26, "args_ptrmask": 0x02aa801a})

    def test_packed_scalar_spills_do_not_shift_gp_pointer_bits(self) -> None:
        spills = [RegisterSpill(index, offset, size) for index, offset, size in (
            (0, 8, 8), (1, 16, 8), (2, 24, 8), (3, 32, 8), (4, 40, 4),
            (5, 44, 4), (6, 48, 8), (7, 56, 8), (8, 64, 8),
        )]
        roots = derive_entry_gc_map(64, 6, 0x2d, spills)
        self.assertEqual(roots["reg_ptrmask"], 0x4d)
        self.assertEqual(roots["stack_args_size"], 0)
        self.assertEqual(roots["stack_ptrmask"], 0)

    def test_narrow_scalar_and_empty_slow_paths(self) -> None:
        stores = self.STORES + bytes.fromhex("40887c2418")
        loads = self.LOADS + bytes.fromhex("0fb67c2418")
        spills = morestack_spills(morestack_code(stores + b"\x90", loads),
                                  0x1320, 0x1300, 0x2000)
        self.assertEqual(spills[-1], RegisterSpill(3, 24, 1))
        self.assertEqual(derive_entry_gc_map(24, 1, 1, spills)["reg_ptrmask"], 1)
        self.assertEqual(morestack_spills(morestack_code(b"", b""), 0x1320, 0x1300, 0x2000), [])
        self.assertEqual(derive_entry_gc_map(32, 4, 0, [])["stack_args_size"], 32)

    def test_rejects_unsupported_or_ambiguous_slow_paths(self) -> None:
        cases = (
            morestack_code(self.STORES, self.LOADS, target=0x2001),
            morestack_code(self.STORES, self.LOADS, entry=0x1301),
            morestack_code(self.STORES, self.LOADS[:-5]),
            morestack_code(self.STORES + self.STORES[:5], self.LOADS),
            morestack_code(bytes.fromhex("4889542408"), bytes.fromhex("488b542408")),  # RDX
            morestack_code(bytes.fromhex("f20f11442408"), bytes.fromhex("f20f10442408")),  # XMM0
            morestack_code(bytes.fromhex("48894008"), self.LOADS),  # wrong stack base
            morestack_code(bytes.fromhex("4883ec08") + self.STORES, self.LOADS),
            morestack_code(self.STORES, self.LOADS)[:-1],
        )
        for code in cases:
            with self.subTest(code=code.hex()), self.assertRaises(ValueError):
                morestack_spills(code, 0x1320, 0x1300, 0x2000)

    def test_rejects_invalid_maps_and_unaccounted_roots(self) -> None:
        cases: tuple[tuple[int, int, int, list[RegisterSpill]], ...] = (
            (8, 2, 1, []), (8, 1, 2, []), (-8, 0, 0, []), (9, 1, 0, []),
            (520, 0, 0, []), (16, 2, 1, [RegisterSpill(0, 8, 4)]),
            (16, 2, 2, [RegisterSpill(0, 8, 8)]),
            (16, 2, 0, [RegisterSpill(0, 8, 8), RegisterSpill(1, 12, 4)]),
            (8, 1, 0, [RegisterSpill(0, 16, 8)]),
            (16, 2, 0, [RegisterSpill(0, 8, 8), RegisterSpill(0, 16, 8)]),
        )
        for args, nbit, mask, spills in cases:
            with self.subTest(args=args, nbit=nbit, mask=mask, spills=spills), self.assertRaises(ValueError):
                derive_entry_gc_map(args, nbit, mask, spills)

    @classmethod
    def image(cls, changes: tuple[tuple[int, bytes], ...] = ()) -> GoImage:
        data = bytearray(ELFReaderTests.image().ljust(1024, b"\0"))
        struct.pack_into("<QQ", data, 64 + 32, len(data), len(data))  # PT_LOAD file/memory sizes
        struct.pack_into("<I", data, 128, 0x300)  # _func.entryOff
        struct.pack_into("<i", data, 128 + 8, 16)
        struct.pack_into("<I", data, 128 + 28, 2)  # skip two pcdata words
        data[128 + 43] = 1
        struct.pack_into("<I", data, 128 + 52, 0)  # ArgsPointerMaps offset from gofunc
        struct.pack_into("<ii", data, 512, 2, 1)
        data[520:522] = b"\x01\x00"  # index zero is live; index one is dead
        code = bytes.fromhex("493b6610761a554889e54883ec28")
        data[768:768 + len(code)] = code
        path = morestack_code(cls.STORES, cls.LOADS)
        data[800:800 + len(path)] = path
        for offset, replacement in changes:
            data[offset:offset + len(replacement)] = replacement
        image = GoImage.__new__(GoImage)
        image.d = bytes(data)
        image.elf = ELF(image.d)
        image.ptr_size, image.text_base, image.md_vaddr = 8, 0x1000, 0x1000
        setattr(image, "_function_records", {0x1300: (128, 0x1320 + len(path))})
        setattr(image, "_morestack_noctxt", 0x2000)
        setattr(image.elf, "relative_relocs", ((0x1000 + 344, 0x1200),))
        return image

    def test_reads_original_argument_bitmap_zero_through_elf_mapping(self) -> None:
        roots = self.image().entry_gc_map(0x1300)
        self.assertEqual((roots["reg_ptrmask"], roots["stack_args_size"], roots["stack_ptrmask"]),
                         (1, 0, 0))
        self.assertEqual((roots["args_size"], roots["args_nbit"], roots["args_ptrmask"]), (16, 1, 1))

    def test_rejects_missing_truncated_or_invalid_original_maps(self) -> None:
        changes = (
            ((128 + 43, b"\x00"),),
            ((128 + 52, struct.pack("<I", 0xffffffff)),),
            ((128 + 52, struct.pack("<I", 0x200)),),  # outside PT_LOAD
            ((128 + 28, struct.pack("<I", 1024)),),
            ((512, struct.pack("<i", 0)),),
            ((516, struct.pack("<i", 3)),),
            ((512, struct.pack("<i", 10000)),),
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.image(change).entry_gc_map(0x1300)


class PlannerItabTests(unittest.TestCase):
    GETTER = bytes.fromhex("4885c07415488d0df42f0000483948587508488b4860488b01c331c0c3")

    def test_complete_getter_proves_itab_and_field_offsets(self) -> None:
        self.assertEqual(planner_response_itab(self.GETTER, 0x1000), 0x4000)

    def test_rejects_changed_offsets_branches_and_truncation(self) -> None:
        for offset, value in ((15, 0x50), (21, 0x68), (17, 7), (3, 0x75), (25, 0x90)):
            code = bytearray(self.GETTER)
            code[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                planner_response_itab(bytes(code), 0x1000)
        for size in range(len(self.GETTER)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                planner_response_itab(self.GETTER[:size], 0x1000)


BUILDSYMBOLS_PROLOGUES = {0x1000: "554889e54883ec28", 0x2000: "554889e54881ec00020000"}
FIXTURE_GC_MAP: EntryGCMap = {"reg_ptrmask": 1, "stack_args_size": 0, "stack_ptrmask": 0,
                             "args_size": 16, "args_nbit": 1, "args_ptrmask": 1}
BUILDSYMBOLS_ROWS: list[Book] = [
    {"id": "small", "symbol": "example.small", "mode": "async", "mech": "fullcgo", "retcap": 0},
    {"id": "large", "symbol": "example.large", "mode": "async", "mech": "asmcgo", "retcap": 0},
]
BUILDSYMBOLS_FUNCTIONS = [(0x3000, "runtime.cgocall"), (0x2000, "example.large"),
             (0x4000, "runtime.asmcgocall"), (0x1000, "example.small")]


class BuildIdentity:
    def build_id(self) -> str:
        return "fixture-build-id"


@dataclass
class FixtureImage:
    elf: BuildIdentity = field(default_factory=BuildIdentity)
    ptr_size: int = 8
    md_vaddr: int = 0x5000
    text_base: int = 0x1000
    minpc: int = 0x1000
    nfunc: int = len(BUILDSYMBOLS_FUNCTIONS)
    functions: Callable[[], Iterator[tuple[int, str]]] = lambda: iter(BUILDSYMBOLS_FUNCTIONS)
    prologue_skip: Callable[[int], int] = lambda address: 6 if address in BUILDSYMBOLS_PROLOGUES else 0
    frame_prologue_hex: Callable[[int, int], str] = lambda address, skip: BUILDSYMBOLS_PROLOGUES.get(address, "")
    entry_gc_map: Callable[[int], EntryGCMap] = lambda address: FIXTURE_GC_MAP.copy()
    planner_response_itab: Callable[[], int] = lambda: 0x6000


def fixture_image() -> FixtureImage:
    return FixtureImage()


class BuildSymbolsTests(unittest.TestCase):
    def test_intact_prologues_only(self) -> None:
        for original in BUILDSYMBOLS_PROLOGUES.values():
            with self.subTest(prologue=original):
                prologue = bytes.fromhex(original)
                data = bytes(16) + prologue + bytes(16)
                elf = code_image(data)
                self.assertEqual(frame_prologue_hex(elf, data, 8, 8), original)
                patched = data[:16] + b"\xff\x25\x01\x00\x00\x00" + data[22:]
                self.assertEqual(frame_prologue_hex(elf, patched, 8, 8), "")
                marked = patched[:22] + prologue[5:6] + patched[23:]
                self.assertEqual(frame_prologue_hex(elf, marked, 8, 8), "")

    def test_resolve_and_write(self) -> None:
        with mock.patch.object(build_symbols, "GoImage", return_value=fixture_image()):
            metadata, funcmap = build_symbols.resolve_symbols(b"fixture", "vendor/libagy.so", BUILDSYMBOLS_ROWS)
        self.assertEqual(metadata["agy_path"], "vendor/libagy.so")
        self.assertEqual(metadata["prologues"]["example.small"], BUILDSYMBOLS_PROLOGUES[0x1000])
        self.assertEqual(metadata["prologues"]["example.large"], BUILDSYMBOLS_PROLOGUES[0x2000])
        self.assertEqual(metadata["missing"], [])
        self.assertEqual(metadata["gc_maps"]["example.small"], FIXTURE_GC_MAP)
        self.assertEqual(funcmap, sorted(BUILDSYMBOLS_FUNCTIONS))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "symbols.json"
            build_symbols.write_symbols(metadata, funcmap, str(output))
            self.assertEqual(json_loads_object(output.read_text()), metadata)
            with gzip.open(output.with_name("funcmap.tsv.gz"), "rt", encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "".join(f"{addr:x}\t{name}\n" for addr, name in funcmap))

    def test_failed_validation_preserves_outputs(self) -> None:
        for failure in ("missing", "patched", "nosplit", "gc"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                image_path, output, funcmap = (directory / name for name in
                                               ("agy.raw", "symbols.json", "funcmap.tsv.gz"))
                for path in (image_path, output, funcmap):
                    path.write_bytes(b"original " + path.name.encode())
                before = {path: path.read_bytes() for path in (image_path, output, funcmap)}
                image = fixture_image()
                if failure == "missing":
                    image.functions = lambda: iter(BUILDSYMBOLS_FUNCTIONS[:-1])
                elif failure == "patched":
                    image.frame_prologue_hex = lambda address, skip: ""
                elif failure == "nosplit":
                    image.prologue_skip = lambda address: 0
                else:
                    def invalid_gc(address: int) -> EntryGCMap:
                        raise ValueError(f"unsupported spill at {address:#x}")
                    image.entry_gc_map = invalid_gc
                with (mock.patch.object(build_symbols, "GoImage", return_value=image),
                      mock.patch.object(build_symbols, "load_procdef", return_value=BUILDSYMBOLS_ROWS),
                      mock.patch.object(sys, "argv", ["build_symbols", str(image_path), str(output)])):
                    with self.assertRaisesRegex(SystemExit, "FAILED verification"):
                        build_symbols.main()
                self.assertEqual({path: path.read_bytes() for path in before}, before)
                self.assertEqual(set(directory.iterdir()), set(before))


ROW: Book = {"id": "fixture", "symbol": "example.(*Fixture).Run", "mech": "fullcgo",
             "mode": "async", "retcap": 0}


class GenSourcesTests(unittest.TestCase):
    def test_frame_boundaries(self) -> None:
        cases = (
            ("554889e54883ec00", 0),
            ("554889e54883ec7f", 127),
            ("554889e54881ec80000000", 128),
            ("554889e54881ecffffff7f", 0x7fffffff),
        )
        for prologue, frame in cases:
            with self.subTest(frame=frame):
                self.assertEqual(gen_sources.frame_size(ROW, prologue), frame)

    def test_representative_go_prologues(self) -> None:
        cases = (
            ("smoke", "os.Getenv", "asmcgo", "554889e54883ec10", 0x10),
            ("readlink_filter", "os.readlink", "fullcgo", "554889e54883ec78", 0x78),
            ("tls_write", "crypto/tls.(*Conn).Write", "asmcgo", "554889e54881ecc0000000", 0xc0),
            ("send_user_msg", "example.(*Backend).SendUserMessage", "fullcgo",
             "554889e54881ec18010000", 0x118),
        )
        for book, symbol, _mech, prologue, frame in cases:
            with self.subTest(book=book):
                row: BookTarget = {"id": book, "symbol": symbol}
                self.assertEqual(gen_sources.frame_size(row, prologue), frame)

    def test_reject_invalid_or_noncanonical_prologues(self) -> None:
        cases = (
            "", "not hex", "554889e54883ec1",  # invalid hex
            "554889e54883ec", "554889e54883ec1000",  # wrong imm8 lengths
            "554889e54881ec000100", "554889e54881ec0001000000",  # wrong imm32 lengths
            "544889e54883ec10", "554889e54883c410",  # wrong PUSH / ADD instead of SUB
            "55488bec4883ec10",  # equivalent MOV with the noncanonical load opcode
            "554889e54881c400010000",  # wrong wide SUB prefix
            "554889e54883ec80", "554889e54883ecff",  # negative imm8
            "554889e54881ec00000080", "554889e54881ecffffffff",  # negative imm32
            "554889e54881ec00000000", "554889e54881ec01000000",
            "554889e54881ec7f000000",  # wide encodings that GAS would shorten
        )
        for prologue in cases:
            with self.subTest(prologue=prologue):
                with self.assertRaises(SystemExit) as failure:
                    gen_sources.frame_size(ROW, prologue)
                self.assertIn(f"book {ROW['id']} ({ROW['symbol']}):", str(failure.exception))

    def test_invalid_later_row_preserves_header(self) -> None:
        later = {"id": "later", "symbol": "example.Later", "mech": "asmcgo"}
        metadata = {"build_id": "fixture", "planner_response_itab": 0x6000,
                    "gc_maps": {ROW["symbol"]: FIXTURE_GC_MAP}, "prologues": {
            ROW["symbol"]: "554889e54883ec10", later["symbol"]: "554889e54881ec10000000",
        }}
        with tempfile.TemporaryDirectory() as directory:
            symbols, header = (Path(directory) / name for name in ("symbols.json", "symbols_gen.h"))
            symbols.write_text(json.dumps(metadata))
            header.write_text("existing valid header\n")
            with mock.patch.object(gen_sources, "load_procdef", return_value=[ROW, later]):
                with self.assertRaisesRegex(SystemExit, "book later"):
                    gen_sources.gen_header(str(symbols), str(header))
            self.assertEqual(header.read_text(), "existing valid header\n")

    def test_header_lists_only_stubbed_rows(self) -> None:
        disabled = {"id": "disabled", "symbol": "example.Disabled", "mech": "off"}
        later = {"id": "later", "symbol": "example.Later", "mech": "asmcgo"}
        metadata = {"build_id": "fixture-build", "planner_response_itab": 0x6000, "gc_maps": {
            ROW["symbol"]: FIXTURE_GC_MAP, later["symbol"]: FIXTURE_GC_MAP,
        }, "prologues": {
            ROW["symbol"]: "554889e54883ec10", later["symbol"]: "554889e54881ecc0000000",
        }}
        with tempfile.TemporaryDirectory() as directory:
            symbols, header = (Path(directory) / name for name in ("symbols.json", "symbols_gen.h"))
            symbols.write_text(json.dumps(metadata))
            with mock.patch.object(gen_sources, "load_procdef", return_value=[ROW, disabled, later]):
                gen_sources.gen_header(str(symbols), str(header))
            content = header.read_text()
        self.assertIn('inline constexpr char AGY_BUILD_ID[] = "fixture-build";', content)
        self.assertIn('AGY_PLANNER_RESPONSE_ITAB = 0x6000;', content)
        self.assertIn('#define AGY_STUBS(X) \\\n    X("fixture", 0x10, 0x1, 0, 0x0) \\\n    X("later", 0xc0, 0x1, 0, 0x0)\n\n#endif',
                      content)
        for absent in ("disabled", "-1", "AGY_GO_FRAMES", "agy_pc__", "struct ", "std::array"):
            self.assertNotIn(absent, content)

    def test_invalid_gc_map_preserves_existing_header(self) -> None:
        cases: tuple[EntryGCMap, ...] = (
            {**FIXTURE_GC_MAP, "reg_ptrmask": 1 << 9},
            {**FIXTURE_GC_MAP, "stack_args_size": 520},
            {**FIXTURE_GC_MAP, "stack_args_size": 7},
            {**FIXTURE_GC_MAP, "stack_ptrmask": 1},
            {**FIXTURE_GC_MAP, "stack_ptrmask": -1},
        )
        for roots in cases:
            with self.subTest(roots=roots), tempfile.TemporaryDirectory() as directory:
                symbols, header = (Path(directory) / name for name in ("symbols.json", "symbols_gen.h"))
                symbols.write_text(json.dumps({"planner_response_itab": 0x6000,
                                                "prologues": {ROW["symbol"]: "554889e54883ec10"},
                                                "gc_maps": {ROW["symbol"]: roots}}))
                header.write_text("existing valid header\n")
                with mock.patch.object(gen_sources, "load_procdef", return_value=[ROW]):
                    with self.assertRaisesRegex(SystemExit, "invalid entry GC bitmap"):
                        gen_sources.gen_header(str(symbols), str(header))
                self.assertEqual(header.read_text(), "existing valid header\n")


MAKELIBAGY_ROWS: list[BookTarget] = [{"id": "small", "symbol": "example.small"},
        {"id": "large", "symbol": "example.large"}]
MAKELIBAGY_PROLOGUES = (bytes.fromhex("554889e54883ec28"),
             bytes.fromhex("554889e54881ec00020000"))
MAKELIBAGY_BOOK_OFFSETS = (32, 64)
MAKELIBAGY_PLT_OFFSET = 128
MAKELIBAGY_GOT_OFFSET = 160


@dataclass
class FixtureRelocation:
    address: int
    type: int
    addend: int
    symbol: str


@dataclass
class PatchImage:
    data: bytearray
    metadata: PatchMetadata
    dynamic: dict[str, int]
    symbols: dict[str, dict[str, int]]
    relocations: list[FixtureRelocation]
    sections: dict[str, dict[str, int]]
    mappings: list[tuple[int, int, int]]

    @property
    def elf(self) -> PatchImage:
        return self

    def sec(self, name: str) -> dict[str, int] | None:
        return self.sections.get(name)

    def dynamic_tags(self) -> dict[str, int]:
        return self.dynamic

    def symbol(self, name: str) -> dict[str, int] | None:
        return self.symbols.get(name)

    def plt_relocations(self) -> Iterator[FixtureRelocation]:
        return iter(self.relocations)

    def v2o(self, address: int, size: int = 1) -> int:
        for start, offset, filesz in self.mappings:
            if start <= address and address + size <= start + filesz:
                return offset + address - start
        raise ValueError(f"unmapped fixture address {address:#x}")


def patch_image(plt_pc: int=0x2000, got_pc: int=0x3000, book_pcs: tuple[int, int]=(0x1000, 0x1100)):
    """Separate virtual addresses from file offsets so overflow cases stay tiny."""
    data = bytearray(b"\xa5" * 256)
    metadata: PatchMetadata = {"books": {}, "skips": {}, "prologues": {}}
    symbols: dict[str, dict[str, int]] = {}
    mappings: list[tuple[int, int, int]] = []
    for row, pc, offset, prologue in zip(MAKELIBAGY_ROWS, book_pcs, MAKELIBAGY_BOOK_OFFSETS, MAKELIBAGY_PROLOGUES):
        data[offset:offset + len(prologue)] = prologue
        metadata["books"][row["symbol"]] = pc - 6
        metadata["skips"][row["symbol"]] = 6
        metadata["prologues"][row["symbol"]] = prologue.hex()
        symbols["agy_resume__" + row["id"]] = {"st_value": pc + len(prologue)}
        mappings.append((pc, offset, len(prologue)))
    data[MAKELIBAGY_PLT_OFFSET:MAKELIBAGY_PLT_OFFSET + 16] = b"\xcc" * 16
    data[MAKELIBAGY_GOT_OFFSET:MAKELIBAGY_GOT_OFFSET + 16] = bytes(16)
    mappings.extend(((plt_pc, MAKELIBAGY_PLT_OFFSET, 16), (got_pc, MAKELIBAGY_GOT_OFFSET, 16)))
    sections = {".plt.agy": {"sh_addr": plt_pc, "sh_size": 16},
                ".got.plt.agy": {"sh_addr": got_pc, "sh_size": 16}}
    dynamic = {"DT_FLAGS": 8}
    relocations = [FixtureRelocation(address=got_pc + slot * 8,
                                  type=7, addend=0,
                                  symbol="agy_stub__" + row["id"])
                   for slot, row in enumerate(MAKELIBAGY_ROWS)]

    return PatchImage(data, metadata, dynamic, symbols, relocations, sections, mappings)


class MakeLibagyTests(unittest.TestCase):
    def assert_rejected_without_writes(self, image: PatchImage, message: str,
                                       error: type[BaseException] = SystemExit) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "libagy.so"
            original = bytes(image.data)
            path.write_bytes(original)
            with mock.patch.object(make_libagy, "ELF", return_value=image.elf):
                with self.assertRaisesRegex(error, message):
                    make_libagy.patch_static_books(str(path), MAKELIBAGY_ROWS, image.metadata)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_patch_targets_and_preserved_bytes(self) -> None:
        for eager_tag, value in (("DT_FLAGS", 8), ("DT_FLAGS_1", 1), ("DT_BIND_NOW", 0)):
            with self.subTest(eager_tag=eager_tag), tempfile.TemporaryDirectory() as directory:
                image = patch_image()
                image.dynamic.clear()
                image.dynamic[eager_tag] = value
                path = Path(directory) / "libagy.so"
                path.write_bytes(image.data)
                with mock.patch.object(make_libagy, "ELF", return_value=image.elf):
                    self.assertEqual(make_libagy.patch_static_books(str(path), MAKELIBAGY_ROWS, image.metadata), 2)
                patched = path.read_bytes()
                changed: set[int] = set()
                for slot, offset in enumerate(MAKELIBAGY_BOOK_OFFSETS):
                    self.assertEqual(patched[offset], 0xe9)
                    pc = (0x1000, 0x1100)[slot]
                    self.assertEqual(pc + 5 + struct.unpack_from("<i", patched, offset + 1)[0],
                                     0x2000 + slot * 8)
                    self.assertEqual(patched[offset + 5:offset + len(MAKELIBAGY_PROLOGUES[slot])],
                                     b"\xcc" * (len(MAKELIBAGY_PROLOGUES[slot]) - 5))
                    plt = MAKELIBAGY_PLT_OFFSET + slot * 8
                    self.assertEqual(patched[plt:plt + 2], b"\xff\x25")
                    self.assertEqual(0x2000 + slot * 8 + 6 + struct.unpack_from("<i", patched, plt + 2)[0],
                                     0x3000 + slot * 8)
                    self.assertEqual(patched[plt + 6:plt + 8], b"\x66\x90")
                    changed.update(range(offset, offset + len(MAKELIBAGY_PROLOGUES[slot])))
                    changed.update(range(plt, plt + 8))
                self.assertEqual(len(patched), len(image.data))
                self.assertTrue(all(patched[i] == value for i, value in enumerate(image.data)
                                    if i not in changed))

    def test_later_validation_failure_preserves_entire_image(self) -> None:
        for failure in ("prologue", "resume", "binding", "relocation_type", "plt"):
            with self.subTest(failure=failure):
                image = patch_image()
                if failure == "prologue":
                    image.data[MAKELIBAGY_BOOK_OFFSETS[1]] = 0x90
                    message = "original prologue.*large"
                elif failure == "resume":
                    image.symbols["agy_resume__large"]["st_value"] += 1
                    message = "continuation export.*large"
                elif failure == "binding":
                    image.relocations[1].symbol = "agy_stub__small"
                    message = "PLT binding.*large"
                elif failure == "relocation_type":
                    image.relocations[1].type = 6  # R_X86_64_GLOB_DAT
                    message = "PLT binding.*large"
                else:
                    image.data[MAKELIBAGY_PLT_OFFSET + 8] = 0x90
                    message = "PLT entry.*large"
                self.assert_rejected_without_writes(image, message)

    def test_lazy_binding_rejected_without_writes(self) -> None:
        image = patch_image()
        image.dynamic.clear()
        self.assert_rejected_without_writes(image, "requires eager binding")

    def test_out_of_range_jumps_preserve_entire_image(self) -> None:
        # The first book validates, then the second book's backward jump exceeds rel32.
        image = patch_image(book_pcs=(0x1000, 0x2008 + (1 << 31)))
        self.assert_rejected_without_writes(image, "book large:.*signed 32-bit")
        # Both GOT slots exist, but the PLT cannot address them with RIP-relative disp32.
        image = patch_image(got_pc=0x2006 + (1 << 31))
        self.assert_rejected_without_writes(image, "PLT small:.*signed 32-bit")

    def test_rel32_boundaries(self) -> None:
        next_pc = 1 << 40
        for displacement in (-(1 << 31), (1 << 31) - 1):
            with self.subTest(displacement=displacement):
                self.assertEqual(make_libagy.rel32(next_pc + displacement, next_pc, "fixture"),
                                 displacement)
        for displacement in (-(1 << 31) - 1, 1 << 31):
            with self.subTest(displacement=displacement):
                with self.assertRaisesRegex(SystemExit, "fixture:.*signed 32-bit"):
                    make_libagy.rel32(next_pc + displacement, next_pc, "fixture")

    def test_assembler_preserves_fixed_width_at_displacement_boundaries(self) -> None:
        pc = 1 << 40
        for displacement in (-(1 << 31), -1, 0, 1, (1 << 31) - 1):
            with self.subTest(displacement=displacement):
                jump = make_libagy.assemble_book_jump(pc + 5 + displacement, pc, "book fixture")
                self.assertEqual(jump[:1], b"\xe9")
                self.assertEqual(len(jump), 5)
                self.assertEqual(struct.unpack_from("<i", jump, 1)[0], displacement)
                plt = make_libagy.assemble_plt_jump(pc + 6 + displacement, pc, "PLT fixture")
                self.assertEqual(plt[:2], b"\xff\x25")
                self.assertEqual(len(plt), 8)
                self.assertEqual(struct.unpack_from("<i", plt, 2)[0], displacement)
                self.assertEqual(plt[6:], b"\x66\x90")

    def test_later_assembler_failure_preserves_entire_image(self) -> None:
        assemble = make_libagy.assemble_book_jump

        def fail_later(target: int, pc: int, context: str):
            if context == "book large":
                raise RuntimeError("fixture assembler failure")
            return assemble(target, pc, context)

        with mock.patch.object(make_libagy, "assemble_book_jump", side_effect=fail_later):
            self.assert_rejected_without_writes(patch_image(), "fixture assembler failure", RuntimeError)

    def test_relocate_section_headers_moves_mid_file_table_to_aligned_eof(self) -> None:
        sectionless = ELFReaderTests.image()
        self.assertIs(make_libagy.relocate_section_headers(sectionless), sectionless)

        shstrtab = b"\0.shstrtab\0"
        shdr_undef = bytes(64)
        shdr_str = struct.pack("<IIQQQQIIQQ", 1, 3, 0, 0, 128, len(shstrtab), 0, 0, 1, 0)
        table = shdr_undef + shdr_str

        # Already at EOF: returned untouched.
        at_eof = bytearray(sectionless)
        at_eof[128:128 + len(shstrtab)] = shstrtab
        eof_shoff = len(at_eof)
        at_eof.extend(table)
        struct.pack_into("<Q", at_eof, 40, eof_shoff)
        struct.pack_into("<HHH", at_eof, 58, 64, 2, 1)
        at_eof_bytes = bytes(at_eof)
        self.assertIs(make_libagy.relocate_section_headers(at_eof_bytes), at_eof_bytes)

        # Mid-file section header table before trailing segment bytes (BOLT layout).
        mid = bytearray(sectionless.ljust(515, b"\x5a"))
        mid[128:128 + len(shstrtab)] = shstrtab
        mid[192:192 + len(table)] = table
        struct.pack_into("<Q", mid, 40, 192)
        struct.pack_into("<HHH", mid, 58, 64, 2, 1)
        relocated = make_libagy.relocate_section_headers(bytes(mid))
        self.assertEqual(len(relocated), 520 + len(table))
        self.assertEqual(struct.unpack_from("<Q", relocated, 40)[0], 520)
        self.assertEqual(relocated[:40], bytes(mid[:40]))
        self.assertEqual(relocated[48:515], bytes(mid[48:515]))
        self.assertEqual(relocated[515:520], b"\0" * 5)
        self.assertEqual(relocated[520:], table)
        sec = ELF(relocated).sec(".shstrtab")
        self.assertIsNotNone(sec)
        assert sec is not None
        self.assertEqual(sec["sh_size"], len(shstrtab))

        # Truncated section header table is rejected.
        truncated = bytearray(mid)
        struct.pack_into("<Q", truncated, 40, len(truncated) - 64)
        with self.assertRaisesRegex(SystemExit, "section header table extends past end"):
            make_libagy.relocate_section_headers(bytes(truncated))


if __name__ == "__main__":
    unittest.main()
