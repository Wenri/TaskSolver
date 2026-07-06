"""setup.py — metadata lives in pyproject.toml; this adds the shim build hook.

The antigravity LD_PRELOAD shim (antigravity/vendor/antigravity.so) is a main,
x86-64-specific target of this repo, so it's built as part of the tasksolver
package build — one `pixi install` produces everything, and the package is
arch-specific (linux-64; see [tool.pixi.package.build.config] noarch=false).

The build is REQUIRED: if it fails (missing gcc, frida-gum fetch failure, etc.)
the tasksolver build fails loudly. Set ANTIGRAVITY_SKIP_BUILD=1 to opt out (e.g.
to build the pure-Python library on a host without the native toolchain). Note
this hook only runs when building tasksolver from source; installing a prebuilt
wheel/conda package does not trigger it.
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
        if os.environ.get("ANTIGRAVITY_SKIP_BUILD"):
            sys.stderr.write("[setup] ANTIGRAVITY_SKIP_BUILD set — skipping antigravity.so build\n")
            return
        if not os.path.isdir(os.path.join(root, "antigravity")):
            return  # antigravity/ absent (e.g. partial checkout) — nothing to build
        # CMake + Ninja (both pixi host-dependencies) run the whole chain: configure fetches agy
        # (sha512-verified) + the frida-gum devkit into vendor/, then the build generates
        # symbols_gen.h from the committed symbols.json and compiles the shim. All idempotent;
        # picks up conda's g++/python3-config from the env ($CXX / $CONDA_PREFIX).
        build = os.path.join("antigravity", "build")
        subprocess.run(["cmake", "-S", "antigravity", "-B", build, "-G", "Ninja"], cwd=root, check=True)
        subprocess.run(["cmake", "--build", build], cwd=root, check=True)
        sys.stderr.write("[setup] antigravity.so built (arch-specific tasksolver build)\n")


setup(cmdclass={"build_py": BuildPyWithShim})
