"""Offline metadata checks; run from antigravity/ with
``pixi run python -m unittest symbols.test_build_symbols``.
"""
import gzip
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from . import build_symbols
from .agyelf import frame_prologue_hex


PROLOGUES = {0x1000: "554889e54883ec28", 0x2000: "554889e54881ec00020000"}
ROWS = [
    {"id": "small", "symbol": "example.small", "mode": "async", "mech": "fullcgo", "retcap": 0},
    {"id": "large", "symbol": "example.large", "mode": "async", "mech": "asmcgo", "retcap": 0},
]
FUNCTIONS = [(0x3000, "runtime.cgocall"), (0x2000, "example.large"),
             (0x4000, "runtime.asmcgocall"), (0x1000, "example.small")]


def fixture_image():
    return SimpleNamespace(
        elf=SimpleNamespace(build_id=lambda: "fixture-build-id"),
        ptr_size=8, md_vaddr=0x5000, text_base=0x1000, minpc=0x1000, nfunc=len(FUNCTIONS),
        functions=lambda: iter(FUNCTIONS), is_prologue=lambda address: True,
        prologue_skip=lambda address: 6 if address in PROLOGUES else 0,
        frame_prologue_hex=lambda address, skip: PROLOGUES.get(address, ""),
    )


class BuildSymbolsTests(unittest.TestCase):
    def test_intact_prologues_only(self):
        elf = SimpleNamespace(v2o=lambda address: address)
        for original in PROLOGUES.values():
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
            metadata, funcmap = build_symbols.resolve_symbols(b"fixture", "vendor/libagy.so", ROWS)
        self.assertEqual(metadata["agy_path"], "vendor/libagy.so")
        self.assertEqual(metadata["prologues"]["example.small"], PROLOGUES[0x1000])
        self.assertEqual(metadata["prologues"]["example.large"], PROLOGUES[0x2000])
        self.assertEqual(metadata["missing"], [])
        self.assertEqual(funcmap, sorted(FUNCTIONS))
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
                    image.functions = lambda: iter(FUNCTIONS[:-1])
                elif failure == "patched":
                    image.frame_prologue_hex = lambda address, skip: ""
                else:
                    image.prologue_skip = lambda address: 0
                with (mock.patch.object(build_symbols, "GoImage", return_value=image),
                      mock.patch.object(build_symbols, "load_procdef", return_value=ROWS),
                      mock.patch.object(sys, "argv", ["build_symbols", str(image_path), str(output)])):
                    with self.assertRaisesRegex(SystemExit, "FAILED verification"):
                        build_symbols.main()
                self.assertEqual({path: path.read_bytes() for path in before}, before)
                self.assertEqual(set(directory.iterdir()), set(before))


if __name__ == "__main__":
    unittest.main()
