"""setup.py — metadata lives in pyproject.toml; this only adds a build hook.

Per an explicit choice, the antigravity LD_PRELOAD shim (antigravity/build/
antigravity.so) is built as part of the tasksolver package build, so a single
`pixi install` produces everything.

IMPORTANT: the shim is a machine-local, agy-build-id-pinned dev artifact (it needs
the local `agy` binary, the frida-gum devkit, and system gcc/libpython). So the
step is **fail-soft**: if any of that is missing — e.g. an external consumer
installing tasksolver as a plain library — it warns and skips, leaving the
tasksolver install itself unaffected. Rebuild the shim explicitly after an agy
update with `pixi run antigravity` (that path also re-resolves symbols).
"""
import os
import subprocess
import sys

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithShim(build_py):
    def run(self):
        super().run()
        self._build_antigravity_shim()

    def _build_antigravity_shim(self):
        root = os.path.dirname(os.path.abspath(__file__))
        if not os.path.isdir(os.path.join(root, "antigravity")):
            return
        try:
            # setup.sh: vendor agy + fetch frida-gum devkit + UAPI headers (idempotent).
            subprocess.run(["bash", "antigravity/setup.sh"], cwd=root, check=True)
            # build.sh: gen symbols_gen.h from the committed symbols.json + compile.
            subprocess.run(["bash", "antigravity/native/build.sh"], cwd=root, check=True)
            sys.stderr.write("[setup] antigravity.so built (folded into tasksolver build)\n")
        except Exception as e:  # noqa: BLE001 — never fail the tasksolver install over the shim
            sys.stderr.write(
                f"[setup] skipping antigravity shim build ({e}); "
                "tasksolver install continues. Build it later with `pixi run antigravity`.\n"
            )


setup(cmdclass={"build_py": BuildPyWithShim})
