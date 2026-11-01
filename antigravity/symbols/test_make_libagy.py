"""Offline book patching checks; run from antigravity/ with
``pixi run python -m unittest symbols.test_make_libagy``.
"""
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import lief

from . import make_libagy


ROWS = [{"id": "small", "symbol": "example.small"},
        {"id": "large", "symbol": "example.large"}]
PROLOGUES = (bytes.fromhex("554889e54883ec28"),
             bytes.fromhex("554889e54881ec00020000"))
BOOK_OFFSETS = (32, 64)
PLT_OFFSET = 128
GOT_OFFSET = 160


def fixture(plt_pc=0x2000, got_pc=0x3000, book_pcs=(0x1000, 0x1100)):
    """Separate virtual addresses from file offsets so overflow cases stay tiny."""
    data = bytearray(b"\xa5" * 256)
    metadata = {"books": {}, "skips": {}, "prologues": {}}
    symbols, mappings = {}, []
    for row, pc, offset, prologue in zip(ROWS, book_pcs, BOOK_OFFSETS, PROLOGUES):
        data[offset:offset + len(prologue)] = prologue
        metadata["books"][row["symbol"]] = pc - 6
        metadata["skips"][row["symbol"]] = 6
        metadata["prologues"][row["symbol"]] = prologue.hex()
        symbols["agy_resume__" + row["id"]] = SimpleNamespace(value=pc + len(prologue))
        mappings.append((pc, offset, len(prologue)))
    data[PLT_OFFSET:PLT_OFFSET + 16] = b"\xcc" * 16
    data[GOT_OFFSET:GOT_OFFSET + 16] = bytes(16)
    mappings.extend(((plt_pc, PLT_OFFSET, 16), (got_pc, GOT_OFFSET, 16)))
    sections = {".plt.agy": SimpleNamespace(virtual_address=plt_pc, size=16),
                ".got.plt.agy": SimpleNamespace(virtual_address=got_pc, size=16)}
    dynamic = {lief.ELF.DynamicEntry.TAG.FLAGS: SimpleNamespace(value=8)}
    relocations = [SimpleNamespace(address=got_pc + slot * 8,
                                  type=lief.ELF.Relocation.TYPE.X86_64_JUMP_SLOT,
                                  has_symbol=True,
                                  symbol=SimpleNamespace(name="agy_stub__" + row["id"]))
                   for slot, row in enumerate(ROWS)]

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
                    make_libagy.patch_static_books(path, ROWS, image.metadata)
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
                    self.assertEqual(make_libagy.patch_static_books(path, ROWS, image.metadata), 2)
                patched = path.read_bytes()
                changed = set()
                for slot, offset in enumerate(BOOK_OFFSETS):
                    self.assertEqual(patched[offset], 0xe9)
                    pc = (0x1000, 0x1100)[slot]
                    self.assertEqual(pc + 5 + struct.unpack_from("<i", patched, offset + 1)[0],
                                     0x2000 + slot * 8)
                    self.assertEqual(patched[offset + 5:offset + len(PROLOGUES[slot])],
                                     b"\xcc" * (len(PROLOGUES[slot]) - 5))
                    plt = PLT_OFFSET + slot * 8
                    self.assertEqual(patched[plt:plt + 2], b"\xff\x25")
                    self.assertEqual(0x2000 + slot * 8 + 6 + struct.unpack_from("<i", patched, plt + 2)[0],
                                     0x3000 + slot * 8)
                    self.assertEqual(patched[plt + 6:plt + 8], b"\x66\x90")
                    changed.update(range(offset, offset + len(PROLOGUES[slot])))
                    changed.update(range(plt, plt + 8))
                self.assertEqual(len(patched), len(image.data))
                self.assertTrue(all(patched[i] == value for i, value in enumerate(image.data)
                                    if i not in changed))

    def test_later_validation_failure_preserves_entire_image(self):
        for failure in ("prologue", "resume", "binding", "relocation_type", "plt"):
            with self.subTest(failure=failure):
                image = fixture()
                if failure == "prologue":
                    image.data[BOOK_OFFSETS[1]] = 0x90
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
                    image.data[PLT_OFFSET + 8] = 0x90
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
