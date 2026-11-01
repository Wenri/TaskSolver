"""Offline checks for the symbols tools: the procdef parser, the resolver, the generated
sources and the library conversion. No agy binary, no network.

Run with ``pixi run python test_scripts/test_agy_symbols.py``.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import gzip
import json
import struct
import sys
import tempfile
import unittest

import lief

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "antigravity"))

from symbols import build_symbols, gen_sources, make_libagy           # noqa: E402
from symbols.agyelf import (                                           # noqa: E402
    active_rows, frame_prologue_hex, load_procdef, parse_procdef)
from symbols.gen_sources import BOOKS_OUT, render_books                # noqa: E402


class ProcdefTests(unittest.TestCase):
    def test_wrappers_and_symbol_literals(self):
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

    def test_checked_in_python_mirror(self):
        self.assertEqual(render_books(load_procdef()), Path(BOOKS_OUT).read_text())


BUILDSYMBOLS_PROLOGUES = {0x1000: "554889e54883ec28", 0x2000: "554889e54881ec00020000"}
BUILDSYMBOLS_ROWS = [
    {"id": "small", "symbol": "example.small", "mode": "async", "mech": "fullcgo", "retcap": 0},
    {"id": "large", "symbol": "example.large", "mode": "async", "mech": "asmcgo", "retcap": 0},
]
BUILDSYMBOLS_FUNCTIONS = [(0x3000, "runtime.cgocall"), (0x2000, "example.large"),
             (0x4000, "runtime.asmcgocall"), (0x1000, "example.small")]


def fixture_image():
    return SimpleNamespace(
        elf=SimpleNamespace(build_id=lambda: "fixture-build-id"),
        ptr_size=8, md_vaddr=0x5000, text_base=0x1000, minpc=0x1000, nfunc=len(BUILDSYMBOLS_FUNCTIONS),
        functions=lambda: iter(BUILDSYMBOLS_FUNCTIONS), is_prologue=lambda address: True,
        prologue_skip=lambda address: 6 if address in BUILDSYMBOLS_PROLOGUES else 0,
        frame_prologue_hex=lambda address, skip: BUILDSYMBOLS_PROLOGUES.get(address, ""),
    )


class BuildSymbolsTests(unittest.TestCase):
    def test_intact_prologues_only(self):
        elf = SimpleNamespace(v2o=lambda address: address)
        for original in BUILDSYMBOLS_PROLOGUES.values():
            with self.subTest(prologue=original):
                prologue = bytes.fromhex(original)
                data = bytes(16) + prologue + bytes(16)
                self.assertEqual(frame_prologue_hex(elf, data, 8, 8), original)
                patched = data[:16] + b"\xff\x25\x01\x00\x00\x00" + data[22:]
                self.assertEqual(frame_prologue_hex(elf, patched, 8, 8), "")
                marked = patched[:22] + prologue[5:6] + patched[23:]
                self.assertEqual(frame_prologue_hex(elf, marked, 8, 8), "")

    def test_resolve_and_write(self):
        with mock.patch.object(build_symbols, "GoImage", return_value=fixture_image()):
            metadata, funcmap = build_symbols.resolve_symbols(b"fixture", "vendor/libagy.so", BUILDSYMBOLS_ROWS)
        self.assertEqual(metadata["agy_path"], "vendor/libagy.so")
        self.assertEqual(metadata["prologues"]["example.small"], BUILDSYMBOLS_PROLOGUES[0x1000])
        self.assertEqual(metadata["prologues"]["example.large"], BUILDSYMBOLS_PROLOGUES[0x2000])
        self.assertEqual(metadata["missing"], [])
        self.assertEqual(funcmap, sorted(BUILDSYMBOLS_FUNCTIONS))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "symbols.json"
            build_symbols.write_symbols(metadata, funcmap, output)
            self.assertEqual(json.loads(output.read_text()), metadata)
            with gzip.open(output.with_name("funcmap.tsv.gz"), "rt", encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "".join(f"{addr:x}\t{name}\n" for addr, name in funcmap))

    def test_failed_validation_preserves_outputs(self):
        for failure in ("missing", "patched", "nosplit"):
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
                else:
                    image.prologue_skip = lambda address: 0
                with (mock.patch.object(build_symbols, "GoImage", return_value=image),
                      mock.patch.object(build_symbols, "load_procdef", return_value=BUILDSYMBOLS_ROWS),
                      mock.patch.object(sys, "argv", ["build_symbols", str(image_path), str(output)])):
                    with self.assertRaisesRegex(SystemExit, "FAILED verification"):
                        build_symbols.main()
                self.assertEqual({path: path.read_bytes() for path in before}, before)
                self.assertEqual(set(directory.iterdir()), set(before))


ROW = {"id": "fixture", "symbol": "example.(*Fixture).Run", "mech": "fullcgo"}


class GenSourcesTests(unittest.TestCase):
    def test_frame_boundaries(self):
        cases = (
            ("554889e54883ec00", 0),
            ("554889e54883ec7f", 127),
            ("554889e54881ec80000000", 128),
            ("554889e54881ecffffff7f", 0x7fffffff),
        )
        for prologue, frame in cases:
            with self.subTest(frame=frame):
                self.assertEqual(gen_sources.frame_size(ROW, prologue), frame)

    def test_representative_go_prologues(self):
        cases = (
            ("smoke", "os.Getenv", "asmcgo", "554889e54883ec10", 0x10),
            ("readlink_filter", "os.readlink", "fullcgo", "554889e54883ec78", 0x78),
            ("tls_write", "crypto/tls.(*Conn).Write", "asmcgo", "554889e54881ecc0000000", 0xc0),
            ("send_user_msg", "example.(*Backend).SendUserMessage", "fullcgo",
             "554889e54881ec18010000", 0x118),
        )
        for book, symbol, mech, prologue, frame in cases:
            with self.subTest(book=book):
                row = {"id": book, "symbol": symbol, "mech": mech}
                self.assertEqual(gen_sources.frame_size(row, prologue), frame)

    def test_reject_invalid_or_noncanonical_prologues(self):
        cases = (
            "", "not hex", "554889e54883ec1",  # invalid hex
            "554889e54883ec", "554889e54883ec1000",  # wrong imm8 lengths
            "554889e54881ec000100", "554889e54881ec0001000000",  # wrong imm32 lengths
            "544889e54883ec10", "554889e54883c410",  # wrong PUSH / ADD instead of SUB
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

    def test_invalid_later_row_preserves_header(self):
        later = {"id": "later", "symbol": "example.Later", "mech": "asmcgo"}
        metadata = {"build_id": "fixture", "prologues": {
            ROW["symbol"]: "554889e54883ec10", later["symbol"]: "554889e54881ec10000000",
        }}
        with tempfile.TemporaryDirectory() as directory:
            symbols, header = (Path(directory) / name for name in ("symbols.json", "symbols_gen.h"))
            symbols.write_text(json.dumps(metadata))
            header.write_text("existing valid header\n")
            with mock.patch.object(gen_sources, "load_procdef", return_value=[ROW, later]):
                with self.assertRaisesRegex(SystemExit, "book later"):
                    gen_sources.gen_header(symbols, header)
            self.assertEqual(header.read_text(), "existing valid header\n")

    def test_header_lists_only_stubbed_rows(self):
        disabled = {"id": "disabled", "symbol": "example.Disabled", "mech": "off"}
        later = {"id": "later", "symbol": "example.Later", "mech": "asmcgo"}
        metadata = {"build_id": "fixture-build", "prologues": {
            ROW["symbol"]: "554889e54883ec10", later["symbol"]: "554889e54881ecc0000000",
        }}
        with tempfile.TemporaryDirectory() as directory:
            symbols, header = (Path(directory) / name for name in ("symbols.json", "symbols_gen.h"))
            symbols.write_text(json.dumps(metadata))
            with mock.patch.object(gen_sources, "load_procdef", return_value=[ROW, disabled, later]):
                gen_sources.gen_header(symbols, header)
            content = header.read_text()
        self.assertIn('inline constexpr char AGY_BUILD_ID[] = "fixture-build";', content)
        self.assertIn('#define AGY_STUBS(X) \\\n    X("fixture", 0x10) \\\n    X("later", 0xc0)\n\n#endif',
                      content)
        for absent in ("disabled", "-1", "AGY_GO_FRAMES", "agy_pc__", "struct ", "std::array"):
            self.assertNotIn(absent, content)


MAKELIBAGY_ROWS = [{"id": "small", "symbol": "example.small"},
        {"id": "large", "symbol": "example.large"}]
MAKELIBAGY_PROLOGUES = (bytes.fromhex("554889e54883ec28"),
             bytes.fromhex("554889e54881ec00020000"))
MAKELIBAGY_BOOK_OFFSETS = (32, 64)
MAKELIBAGY_PLT_OFFSET = 128
MAKELIBAGY_GOT_OFFSET = 160


def fixture(plt_pc=0x2000, got_pc=0x3000, book_pcs=(0x1000, 0x1100)):
    """Separate virtual addresses from file offsets so overflow cases stay tiny."""
    data = bytearray(b"\xa5" * 256)
    metadata = {"books": {}, "skips": {}, "prologues": {}}
    symbols, mappings = {}, []
    for row, pc, offset, prologue in zip(MAKELIBAGY_ROWS, book_pcs, MAKELIBAGY_BOOK_OFFSETS, MAKELIBAGY_PROLOGUES):
        data[offset:offset + len(prologue)] = prologue
        metadata["books"][row["symbol"]] = pc - 6
        metadata["skips"][row["symbol"]] = 6
        metadata["prologues"][row["symbol"]] = prologue.hex()
        symbols["agy_resume__" + row["id"]] = SimpleNamespace(value=pc + len(prologue))
        mappings.append((pc, offset, len(prologue)))
    data[MAKELIBAGY_PLT_OFFSET:MAKELIBAGY_PLT_OFFSET + 16] = b"\xcc" * 16
    data[MAKELIBAGY_GOT_OFFSET:MAKELIBAGY_GOT_OFFSET + 16] = bytes(16)
    mappings.extend(((plt_pc, MAKELIBAGY_PLT_OFFSET, 16), (got_pc, MAKELIBAGY_GOT_OFFSET, 16)))
    sections = {".plt.agy": SimpleNamespace(virtual_address=plt_pc, size=16),
                ".got.plt.agy": SimpleNamespace(virtual_address=got_pc, size=16)}
    dynamic = {lief.ELF.DynamicEntry.TAG.FLAGS: SimpleNamespace(value=8)}
    relocations = [SimpleNamespace(address=got_pc + slot * 8,
                                  type=lief.ELF.Relocation.TYPE.X86_64_JUMP_SLOT,
                                  has_symbol=True,
                                  symbol=SimpleNamespace(name="agy_stub__" + row["id"]))
                   for slot, row in enumerate(MAKELIBAGY_ROWS)]

    def v2o(address):
        for start, offset, size in mappings:
            if start <= address < start + size:
                return offset + address - start
        raise ValueError(f"unmapped fixture address {address:#x}")

    binary = SimpleNamespace(get_section=sections.get, get=dynamic.get,
                             has=lambda tag: tag in dynamic, get_symbol=symbols.get,
                             pltgot_relocations=relocations)
    return SimpleNamespace(data=data, metadata=metadata, sections=sections, dynamic=dynamic,
                           symbols=symbols, relocations=relocations,
                           elf=SimpleNamespace(bin=binary, v2o=v2o))


class MakeLibagyTests(unittest.TestCase):
    def assert_rejected_without_writes(self, image, message):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "libagy.so"
            original = bytes(image.data)
            path.write_bytes(original)
            with mock.patch.object(make_libagy, "ELF", return_value=image.elf):
                with self.assertRaisesRegex(SystemExit, message):
                    make_libagy.patch_static_books(path, MAKELIBAGY_ROWS, image.metadata)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_patch_targets_and_preserved_bytes(self):
        tags = lief.ELF.DynamicEntry.TAG
        for eager_tag, value in ((tags.FLAGS, 8), (tags.FLAGS_1, 1), (tags.BIND_NOW, 0)):
            with self.subTest(eager_tag=eager_tag), tempfile.TemporaryDirectory() as directory:
                image = fixture()
                image.dynamic.clear()
                image.dynamic[eager_tag] = SimpleNamespace(value=value)
                path = Path(directory) / "libagy.so"
                path.write_bytes(image.data)
                with mock.patch.object(make_libagy, "ELF", return_value=image.elf):
                    self.assertEqual(make_libagy.patch_static_books(path, MAKELIBAGY_ROWS, image.metadata), 2)
                patched = path.read_bytes()
                changed = set()
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

    def test_later_validation_failure_preserves_entire_image(self):
        for failure in ("prologue", "resume", "binding", "relocation_type", "plt"):
            with self.subTest(failure=failure):
                image = fixture()
                if failure == "prologue":
                    image.data[MAKELIBAGY_BOOK_OFFSETS[1]] = 0x90
                    message = "original prologue.*large"
                elif failure == "resume":
                    image.symbols["agy_resume__large"].value += 1
                    message = "continuation export.*large"
                elif failure == "binding":
                    image.relocations[1].symbol.name = "agy_stub__small"
                    message = "PLT binding.*large"
                elif failure == "relocation_type":
                    image.relocations[1].type = lief.ELF.Relocation.TYPE.X86_64_GLOB_DAT
                    message = "PLT binding.*large"
                else:
                    image.data[MAKELIBAGY_PLT_OFFSET + 8] = 0x90
                    message = "PLT entry.*large"
                self.assert_rejected_without_writes(image, message)

    def test_lazy_binding_rejected_without_writes(self):
        image = fixture()
        image.dynamic.clear()
        self.assert_rejected_without_writes(image, "requires eager binding")

    def test_out_of_range_jumps_preserve_entire_image(self):
        # The first book validates, then the second book's backward jump exceeds rel32.
        image = fixture(book_pcs=(0x1000, 0x2008 + (1 << 31)))
        self.assert_rejected_without_writes(image, "book large:.*signed 32-bit")
        # Both GOT slots exist, but the PLT cannot address them with RIP-relative disp32.
        image = fixture(got_pc=0x2006 + (1 << 31))
        self.assert_rejected_without_writes(image, "PLT small:.*signed 32-bit")

    def test_rel32_boundaries(self):
        next_pc = 1 << 40
        for displacement in (-(1 << 31), (1 << 31) - 1):
            with self.subTest(displacement=displacement):
                encoded = make_libagy.rel32(next_pc + displacement, next_pc, "fixture")
                self.assertEqual(struct.unpack("<i", encoded)[0], displacement)
        for displacement in (-(1 << 31) - 1, 1 << 31):
            with self.subTest(displacement=displacement):
                with self.assertRaisesRegex(SystemExit, "fixture:.*signed 32-bit"):
                    make_libagy.rel32(next_pc + displacement, next_pc, "fixture")


if __name__ == "__main__":
    unittest.main()
