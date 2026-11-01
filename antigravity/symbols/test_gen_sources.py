"""Offline stub generation checks; run from antigravity/ with
``pixi run python -m unittest symbols.test_gen_sources``.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from . import gen_sources


ROW = {"id": "fixture", "symbol": "example.(*Fixture).Run", "mech": "fullcgo"}


class GenSourcesTests(unittest.TestCase):
    def test_frame_boundaries_for_both_gateways(self):
        cases = (
            ("554889e54883ec00", 0),
            ("554889e54883ec7f", 127),
            ("554889e54881ec80000000", 128),
            ("554889e54881ecffffff7f", 0x7fffffff),
        )
        for mech in ("fullcgo", "asmcgo"):
            for prologue, frame in cases:
                with self.subTest(mech=mech, frame=frame):
                    row = {**ROW, "mech": mech}
                    self.assertEqual(gen_sources.stub_entry(row, 7, prologue),
                                     f"{mech.upper()}(fixture, 7, {frame:#x})")

    def test_representative_go_prologues(self):
        cases = (
            ("smoke", "os.Getenv", "asmcgo", 0, "554889e54883ec10", "ASMCGO(smoke, 0, 0x10)"),
            ("readlink_filter", "os.readlink", "fullcgo", 3, "554889e54883ec78",
             "FULLCGO(readlink_filter, 3, 0x78)"),
            ("tls_write", "crypto/tls.(*Conn).Write", "asmcgo", 4, "554889e54881ecc0000000",
             "ASMCGO(tls_write, 4, 0xc0)"),
            ("send_user_msg", "example.(*Backend).SendUserMessage", "fullcgo", 12,
             "554889e54881ec18010000", "FULLCGO(send_user_msg, 12, 0x118)"),
        )
        for hook, symbol, mech, book, prologue, expected in cases:
            with self.subTest(hook=hook):
                row = {"id": hook, "symbol": symbol, "mech": mech}
                self.assertEqual(gen_sources.stub_entry(row, book, prologue), expected)

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
                    gen_sources.stub_entry(ROW, 7, prologue)
                self.assertIn(f"hook {ROW['id']} ({ROW['symbol']}):", str(failure.exception))

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
                with self.assertRaisesRegex(SystemExit, "hook later"):
                    gen_sources.gen_header(symbols, header)
            self.assertEqual(header.read_text(), "existing valid header\n")


if __name__ == "__main__":
    unittest.main()
