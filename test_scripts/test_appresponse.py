#!/usr/bin/env python3
"""Offline response capture checks, including native Unicode and large-payload readers.

Run through pixi for its C++23 compiler; no provider or credentials are used.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import unittest
from pathlib import Path
import sys
import tempfile
from collections.abc import Sequence
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)                       # repo root holds the shared `wirecap` package
_ANTIGRAVITY = os.path.join(_REPO, "antigravity")
sys.path.insert(0, _ANTIGRAVITY)
sys.path.insert(0, _REPO)

_failures: list[str] = []


from wirecap.types import Event, JSONObject


def check(cond: object, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_dispatch_records_full_text() -> None:
    print("[offline] dispatch('app_response') records the full untruncated answer")
    from pyagy import agy_process as ap
    captured: list[JSONObject] = []
    with patch.object(ap._rec, "event", captured.append):
        big = "The mitochondria is the powerhouse of the cell. " * 500  # ~24 KB
        ap.dispatch("app_response", 0xdead, big.encode())
    check(len(captured) == 1, "one event emitted")
    check(captured and captured[0].get("kind") == "app_response", "kind is app_response")
    check(captured and captured[0].get("text") == big, "full text stored (no preview truncation)")


def test_legacy_text_can_start_with_packet_magic() -> None:
    from pyagy import agy_process as ap
    records: list[Event] = []
    with patch.object(ap._rec, "event", records.append):
        ap.dispatch("app_response", 42, b"AGTX is a valid legacy answer prefix")
    check(records[0]["text"] == "AGTX is a valid legacy answer prefix", "legacy magic-prefix text preserved")


def _capture(path: str, events: Sequence[JSONObject]) -> None:
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_load_capture() -> None:
    print("[offline] client._load_capture: one pass yields wire turns + app texts")
    from pyagy import client
    d = tempfile.mkdtemp()
    cap = os.path.join(d, "cap.jsonl")
    _capture(cap, [
        {"kind": "genai_turn", "usage": {"totalTokenCount": 10}},
        {"kind": "app_response", "text": "ALPHA BETA GAMMA"},
        {"kind": "app_response", "text": "ALPHA BETA GAMMA"},   # duplicate full snapshot
        {"kind": "app_response", "text": ""},                    # empty → skipped
        {"kind": "callstack", "src": "x", "frames": []},         # unrelated → ignored
    ])
    turns, app_texts, cur = client._load_capture(cap)
    check(len(turns) == 1, "genai_turn extracted")
    check(app_texts == ["ALPHA BETA GAMMA", "ALPHA BETA GAMMA"], "non-empty app_response texts extracted")
    check(cur == 5, "cursor at last line")
    # cursor slicing (multi-turn): nothing new past the end
    _, more, cur2 = client._load_capture(cap, since=cur)
    check(more == [] and cur2 == cur, "cursor resume yields no re-reads")


def test_response_preference() -> None:
    print("[offline] AgyResponse.source / app_text / text preference")
    from pyagy.client import AgyResponse
    # app present → source 'app', text prefers the longest app answer
    r = AgyResponse(text="from-transcript", transcript="from-transcript", turns=[{}],
                    exit_status=0, capture_path=None, workspace="/w", linked=True,
                    app_turns=["short", "the longer full answer"])
    check(r.source == "app", "source == app when app_turns present")
    check(r.app_text == "the longer full answer", "app_text is the longest snapshot")

    # no app, wire turn carries text → 'wire' (and .text is that wire text; .source agrees)
    r2 = AgyResponse(text="the wire answer", transcript="t",
                     turns=[{"text": "the wire answer"}], exit_status=0,
                     capture_path=None, workspace="/w", linked=True, app_turns=[])
    check(r2.source == "wire", "source == wire when wire turns carry text")
    check(r2.app_text == "", "app_text empty without app_turns")

    # nothing decoded → 'transcript'
    r3 = AgyResponse(text="t", transcript="t", turns=[], exit_status=0,
                     capture_path=None, workspace="/w", linked=False, app_turns=[])
    check(r3.source == "transcript", "source == transcript with no decoded events")


def test_import_purity() -> None:
    print("[offline] agy_process (incl. app_response route) stays stdlib-only")
    code = ("import sys; sys.path[:0] = [%r, %r]; import pyagy.agy_process as ap; "
            "assert 'app_response' in ap._ROUTER; "
            "assert 'tasksolver' not in sys.modules; print('pure')" % (_REPO, _ANTIGRAVITY))
    import subprocess
    r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    check(r.returncode == 0 and "pure" in r.stdout, "purity: app_response routed, no tasksolver")
    if r.returncode != 0:
        print(r.stderr)


class NativeResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("x86_64-conda-linux-gnu-c++")
        if compiler is None:
            raise RuntimeError("run through pixi for the native response fixture's C++23 compiler")
        cls.temporary = tempfile.TemporaryDirectory(prefix="agy-response-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.executable = cls.directory / "response-test"
        root, anti = Path(_REPO), Path(_ANTIGRAVITY)
        result = subprocess.run([compiler, "-std=c++23", "-O2", "-Wall", "-Wextra", "-Werror",
                                 "-Wno-missing-field-initializers", "-pthread",
                                 "-I" + str(anti / "src"), "-I" + str(root / "wirecap/native"),
                                 str(anti / "src/gocall.cpp"),
                                 str(root / "test_scripts/fixtures/agy_response.cpp"),
                                 "-o", str(cls.executable)], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    def packet(self, source: str, mode: str = "normal") -> bytes:
        output = self.directory / "response.bin"
        result = subprocess.run([str(self.executable), source, mode, str(output)],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return output.read_bytes()

    def test_native_unicode_and_large_atomic_response_payloads(self) -> None:
        from pyagy import agy_process as ap
        expected = "中文" * 9000
        records: list[Event] = []
        app = self.packet("app_response")
        chunk = self.packet("resp_chunk")
        self.assertGreater(len(app), 16384)
        with patch.object(ap._rec, "event", records.append):
            ap.dispatch("app_response_packet", 42, app)
            assert ap._corr is not None
            with patch.object(ap._corr, "feed_chunk") as feed:
                ap.dispatch("resp_chunk_packet", 42, chunk)
                feed.assert_called_once()
                self.assertEqual(feed.call_args.args[0], chunk[16:])
        self.assertEqual(records[0]["text"], expected)
        self.assertEqual(len(records), 1)

    def test_native_stack_deduplication_is_bounded_under_concurrent_producers(self) -> None:
        self.assertEqual(self.packet("stack"), b"1024")

    def test_native_reader_rejects_invalid_step_objects(self) -> None:
        for mode in ("bad_step", "overflow_step", "other_step", "wrong_oneof", "bad_wrapper", "bad_response", "bad_text", "negative_length"):
            with self.subTest(mode=mode):
                self.assertEqual(self.packet("app_response", mode), b"")

    def test_native_and_bridge_caps_are_explicit_and_preserve_wire_fallback(self) -> None:
        from pyagy import agy_process as ap
        from pyagy.client import AgyResponse
        for source in ("app_response", "resp_chunk"):
            packet = self.packet(source)
            cases = [self.packet(source, "long"), b"", packet[:1], packet[:2], packet[:3],
                     packet[:8], packet[:20], packet[:-1], b"NOPE" + packet[4:],
                     packet + b"extra", packet[:4] + struct.pack("<I", 2) + packet[8:]]
            for data in cases:
                with self.subTest(source=source, size=len(data)):
                    records: list[Event] = []
                    with patch.object(ap._rec, "event", records.append):
                        ap.dispatch(source + "_packet", 42, data)
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]["kind"], "response_capture_error")
                    response = AgyResponse.from_objs([
                        {"kind": "app_response", "text": "older snapshot"},
                        {"kind": "genai_turn", "text": "complete wire answer"}, *records], "terminal",
                        exit_status=0, capture_path=None, workspace="/tmp")
                    self.assertEqual(response.response_capture_errors, records)
                    if source == "app_response":
                        self.assertEqual(response.text, "complete wire answer")
                        self.assertEqual(response.source, "wire")


def main() -> None:
    test_dispatch_records_full_text()
    test_legacy_text_can_start_with_packet_magic()
    test_load_capture()
    test_response_preference()
    test_import_purity()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(NativeResponseTests))
    check(result.wasSuccessful(), "native response regressions")
    print()
    if _failures:
        print(f"FAIL ({len(_failures)}): " + ", ".join(_failures))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
