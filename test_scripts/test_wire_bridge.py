"""Exercise the real embedded bridge's concurrent startup and shutdown offline."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BridgeLifecycleTests(unittest.TestCase):
    def test_one_worker_and_drained_shutdown(self) -> None:
        compiler = shutil.which("x86_64-conda-linux-gnu-c++")
        if compiler is None:
            self.fail("run through pixi for the native compiler")
        with tempfile.TemporaryDirectory(prefix="wire-bridge-test-") as temporary:
            directory = Path(temporary)
            (directory / "bridge_fixture.py").write_text(
                "count = 0\n"
                "def dispatch(kind: str, stream: int, data: bytes) -> bytes | None:\n"
                "    global count\n"
                "    if kind == 'count': count += 1\n"
                "    if kind == 'check': return count.to_bytes(8, 'little')\n")
            config = str(Path(sys.prefix) / "bin/python3-config")
            flags = shlex.split(subprocess.check_output(
                [config, "--includes", "--ldflags", "--embed"], text=True))
            executable = directory / "bridge"
            tag = f"{sys.version_info.major}{sys.version_info.minor}"
            command = [compiler, "-std=c++23", "-O2", "-pthread",
                       "-Wno-deprecated-declarations", "-I" + str(ROOT / "wirecap/native"),
                       "-I" + str(Path(sys.prefix) / "include"),
                       str(ROOT / "test_scripts/fixtures/wire_bridge.cpp"),
                       str(ROOT / "wirecap/native/pybridge.cpp"), *flags,
                       "-lboost_thread", f"-lboost_python{tag}",
                       "-Wl,-rpath," + str(Path(sys.prefix) / "lib"), "-o", str(executable)]
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=90)
            env = dict(os.environ, PYTHONHOME=sys.prefix, PYTHONPATH=str(directory),
                       WIRE_MODULE="bridge_fixture")
            result = subprocess.run([str(executable)], env=env, capture_output=True,
                                    text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stderr.count("worker ready"), 1, result.stderr)


if __name__ == "__main__":
    unittest.main()
