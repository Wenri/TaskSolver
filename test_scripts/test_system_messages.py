#!/usr/bin/env python3
"""Offline system-message capture checks, including the real native memory reader.

No credentials or agy launch. Run through pixi for its C++23 compiler.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ANTI = ROOT / "antigravity"
sys.path[:0] = [str(ANTI), str(ROOT)]


from pyagy.agy_process.system_messages import decode_system_message

from wirecap.types import JSONObject


def encode(text: bytes=b"hello", event: bytes=b"agent_message", *, text_bytes: int | None=None, event_bytes: int | None=None):
    return struct.pack("<4sIQQII", b"AGSM", 1,
                       len(text) if text_bytes is None else text_bytes,
                       len(event) if event_bytes is None else event_bytes,
                       len(text), len(event)) + event + text


class SystemMessageDecoderTests(unittest.TestCase):
    def test_unicode_and_embedded_nul_are_not_preview_truncated(self) -> None:
        text = ("caf\u00e9 \u4e2d\u6587\0 " * 9000).encode()
        obj = decode_system_message("system_message_legacy", 123, encode(text))
        self.assertEqual(obj["kind"], "system_message")
        self.assertEqual(obj["capture_source"], "system_message_legacy")
        self.assertEqual(obj["step_address"], 123)
        self.assertEqual(obj["text"], text.decode())
        self.assertEqual(obj["event_type"], "agent_message")
        self.assertEqual(obj["text_bytes"], len(text))
        self.assertFalse(obj["truncated"])
        self.assertNotIn("conversation_id", obj)

    def test_native_and_bridge_truncation_are_explicit(self) -> None:
        obj = decode_system_message("system_message", 1,
                                    encode(b"short", b"event", text_bytes=100, event_bytes=5000))
        self.assertTrue(obj["truncated"])
        self.assertEqual((obj["captured_text_bytes"], obj["captured_event_type_bytes"]), (5, 5))
        packet = encode(b"message", b"event")
        for size in (32, 34, 37, len(packet) - 1):
            with self.subTest(size=size):
                obj = decode_system_message("system_message", 1, packet[:size])
                self.assertTrue(obj["truncated"])
                text_bytes = obj["captured_text_bytes"]
                event_bytes = obj["captured_event_type_bytes"]
                assert isinstance(text_bytes, int) and isinstance(event_bytes, int)
                self.assertLess(text_bytes + event_bytes, 12)

    def test_invalid_packets_are_rejected(self) -> None:
        packet = encode()
        cases = (b"", packet[:31], b"NOPE" + packet[4:], packet[:4] + struct.pack("<I", 2) + packet[8:],
                 encode(b"hello", text_bytes=1), packet + b"extra")
        for data in cases:
            with self.subTest(data=data[:32]):
                with self.assertRaises(ValueError):
                    decode_system_message("system_message", 1, data)

    def test_router_retains_repeated_observations_and_reports_capture_errors(self) -> None:
        from pyagy import agy_process as ap
        records: list[JSONObject] = []
        with patch.object(ap._rec, "event", records.append):
            for source in ("system_message", "system_message", "system_message_legacy"):
                ap.dispatch(source, 99, encode())
            ap.dispatch("system_message", 99, b"partial")
        self.assertEqual([r["kind"] for r in records], ["system_message"] * 3 + ["system_message_capture_error"])
        self.assertEqual(records[0], records[1])
        self.assertEqual(records[2]["capture_source"], "system_message_legacy")
        self.assertEqual(records[3]["captured_bytes"], 7)

    def test_embedded_imports_are_stdlib_only(self) -> None:
        code = (f"import sys; sys.path[:0] = {[str(ANTI), str(ROOT)]!r}; "
                "import pyagy.agy_process as p; "
                "assert 'system_message' in p._ROUTER; "
                "assert 'system_message_legacy' in p._ROUTER; "
                "assert 'tasksolver' not in sys.modules")
        result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class NativeSystemMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("x86_64-conda-linux-gnu-c++")
        if compiler is None:
            raise unittest.SkipTest("run through pixi; native system-message fixture needs its C++23 compiler")
        cls.temporary = tempfile.TemporaryDirectory(prefix="agy-system-message-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.executable = cls.directory / "system-message-test"
        result = subprocess.run([compiler, "-std=c++23", "-O2", "-Wall", "-Wextra", "-Werror",
                                 "-Wno-missing-field-initializers",
                                 "-I" + str(ANTI / "src"), "-I" + str(ROOT / "wirecap/native"),
                                 str(ANTI / "src/gocall.cpp"),
                                 str(ROOT / "test_scripts/fixtures/agy_system_message.cpp"),
                                 "-o", str(cls.executable)], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    def run_fixture(self, mode: str):
        output = self.directory / (mode + ".bin")
        result = subprocess.run([str(self.executable), mode, str(output)], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return output.read_bytes()

    def test_real_native_reader_and_argument_registers(self) -> None:
        for mode in ("normal", "legacy", "nul", "empty", "long", "long_event"):
            with self.subTest(mode=mode):
                packet = self.run_fixture(mode)
                source = "system_message_legacy" if mode == "legacy" else "system_message"
                obj = decode_system_message(source, 1, packet)
                self.assertEqual(obj["truncated"], mode in ("long", "long_event"))
                if mode == "nul":
                    self.assertEqual(obj["text"], "before\0after")
                elif mode == "empty":
                    self.assertEqual((obj["text"], obj["event_type"]), ("", ""))
                elif mode == "long":
                    self.assertEqual(obj["text_bytes"], 2 << 20)
                    self.assertEqual(len(packet), 1 << 20)
                elif mode == "long_event":
                    self.assertEqual(obj["event_type_bytes"], 8192)
                    self.assertEqual(obj["captured_event_type_bytes"], 4096)
                else:
                    self.assertEqual(obj["text"], "Harness notification: caf\u00e9 \u4e2d\u6587")
                    self.assertEqual(obj["event_type"], "agent_message")

    def test_unreadable_objects_and_other_step_types_are_not_reported(self) -> None:
        for mode in ("other_step", "bad_step", "overflow_step", "bad_wrapper", "bad_message",
                     "bad_text", "bad_event", "negative_length"):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_fixture(mode), b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
