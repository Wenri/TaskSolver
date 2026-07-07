"""setup.py — metadata lives in pyproject.toml; this adds the native build hooks.

Both native targets are built as part of the tasksolver package build, so one `pixi install`
produces everything (the package is arch-specific — linux-64; see
[tool.pixi.package.build.config] noarch=false):

  1. the antigravity LD_PRELOAD shim `antigravity/vendor/antigravity.so` (which also builds the
     shared `wirecap_bridge` static lib), via CMake + Ninja; and
  2. the vendored, wirecap-patched codex CLI `codex/vendor/codex-rs/target/release/codex`
     (gnu-dynamic, linking the bridge from step 1 + libpython), via Cargo. This runs AFTER the
     shim so `libwirecap_bridge.a` exists for codex's build.rs.

Both are REQUIRED and fail the build loudly (missing toolchain, fetch failure, etc.). Opt out of
either with `ANTIGRAVITY_SKIP_BUILD=1` / `CODEX_SKIP_BUILD=1` (e.g. to build the pure-Python
library, or to skip codex's heavy ~150-crate compile, on a host without that toolchain). This
hook only runs when building tasksolver from source; installing a prebuilt package does not
trigger it.
"""
import os
import subprocess
import sys

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyNative(build_py):
    def run(self):
        super().run()
        root = os.path.dirname(os.path.abspath(__file__))
        self._build_antigravity_shim(root)   # builds wirecap_bridge + antigravity.so
        self._build_codex(root)              # links the bridge from the shim step → must follow it

    def _build_antigravity_shim(self, root):
        if os.environ.get("ANTIGRAVITY_SKIP_BUILD"):
            sys.stderr.write("[setup] ANTIGRAVITY_SKIP_BUILD set — skipping antigravity.so build\n")
            return
        if not os.path.isdir(os.path.join(root, "antigravity")):
            return  # antigravity/ absent (e.g. partial checkout) — nothing to build
        # CMake + Ninja (both pixi host-dependencies) run the whole chain: configure fetches agy
        # (sha512-verified) + the frida-gum devkit into vendor/, then the build generates
        # symbols_gen.h from the committed symbols.json and compiles wirecap_bridge + the shim.
        # All idempotent; picks up conda's g++/python3-config from the env ($CXX / $CONDA_PREFIX).
        build = os.path.join("antigravity", "build")
        subprocess.run(["cmake", "-S", "antigravity", "-B", build, "-G", "Ninja"], cwd=root, check=True)
        subprocess.run(["cmake", "--build", build], cwd=root, check=True)
        sys.stderr.write("[setup] antigravity.so + wirecap_bridge built\n")

    def _build_codex(self, root):
        if os.environ.get("CODEX_SKIP_BUILD"):
            sys.stderr.write("[setup] CODEX_SKIP_BUILD set — skipping codex build\n")
            return
        codex_rs = os.path.join(root, "codex", "vendor", "codex-rs")
        if not os.path.isdir(codex_rs):
            return  # codex/ not vendored (e.g. partial checkout) — nothing to build
        # cargo build the codex CLI in the DEFAULT host target (x86_64-unknown-linux-gnu, dynamic
        # glibc — NOT the static-musl release target, which can't embed the pixi libpython). Its
        # build.rs links the wirecap_bridge produced above + libpython. LIBCLANG_PATH lets
        # aws-lc-rs/rustls' bindgen find libclang. Idempotent: cargo no-ops when nothing changed.
        env = dict(os.environ)
        conda = env.get("CONDA_PREFIX")
        if conda and not env.get("LIBCLANG_PATH"):
            env["LIBCLANG_PATH"] = os.path.join(conda, "lib")
        subprocess.run(["cargo", "build", "--release", "-p", "codex-cli"],
                       cwd=codex_rs, env=env, check=True)
        sys.stderr.write("[setup] codex built (gnu-dynamic, wirecap-patched)\n")


setup(cmdclass={"build_py": BuildPyNative})
