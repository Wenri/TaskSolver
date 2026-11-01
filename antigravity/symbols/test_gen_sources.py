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
        for hook, symbol, mech, prologue, frame in cases:
            with self.subTest(hook=hook):
                row = {"id": hook, "symbol": symbol, "mech": mech}
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

    def test_header_frames_preserve_disabled_book_rows(self):
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
        self.assertIn("#include <cstdint>", content)
        self.assertIn("inline constexpr std::int32_t AGY_GO_FRAMES[] = {\n"
                      "    0x10,\n    -1,\n    0xc0,\n};", content)
        for declaration in ("AGY_STUB", "agy_pc__", "struct ", "extern ", "std::array", "goframe.h"):
            self.assertNotIn(declaration, content)


if __name__ == "__main__":
    unittest.main()
