"""setup.py — metadata lives in pyproject.toml; this adds the native build hooks.

Both native targets are built as part of the tasksolver package build, so one `pixi install`
produces everything (the package is arch-specific — linux-64; see
[tool.pixi.package.build.config] noarch=false):

  1. the antigravity LD_PRELOAD shim `antigravity/vendor/antigravity.so` (which also builds the
     shared `wirecap_bridge` static lib), via CMake + Ninja; and
  2. the vendored, wirecap-patched codex CLI `codex/vendor/codex-rs/target/release/codex`
     (gnu-dynamic, linking the bridge from step 1 + libpython), via Cargo. This runs AFTER the
     shim so `libwirecap_bridge.a` exists for codex's build.rs.

Both are REQUIRED and fail the build loudly if the toolchain (cmake/ninja/cargo/boost/libclang/
kernel-headers) or network (the agy download) is missing. There is NO opt-out: skipping either
would ship a wheel whose `agy*`/`codex*` backends don't work — a broken package, not a lighter
one — so a tasksolver build always produces the natives or fails. (This hook only runs when
building from source; installing a prebuilt wheel does not trigger it.)

After building, `_bundle_artifacts` copies the shim + agy into `pyagy/vendor/` and the
(debug-stripped) codex into `pycodex/vendor/` INSIDE the wheel (build_lib), so the wheel is
self-contained — the runtime resolver (`pyagy/pycodex/_env.py:_vendored`) finds them there (or, in
a source/editable checkout, in the sibling `vendor/`). The artifacts are ALWAYS packaged, never
supplied externally — there is no env-var override. `BinaryDistribution` forces the platform+ABI
wheel tag.
"""
import json
import os
import shutil
import subprocess
import sys

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py


class BinaryDistribution(Distribution):
    """Force a non-purelib, platform+ABI-tagged wheel. The natives are produced by the build_py
    hook (not setuptools ext_modules), so setuptools would otherwise tag the wheel py3-none-any;
    has_ext_modules=True flips Root-Is-Purelib to false and yields e.g. cp313-cp313-linux_x86_64,
    which correctly pins the wheel to the interpreter its bundled libpython was built against."""

    def has_ext_modules(self):
        return True


class BuildPyNative(build_py):
    def run(self):
        super().run()
        root = os.path.dirname(os.path.abspath(__file__))
        self._build_antigravity_shim(root)   # builds wirecap_bridge + antigravity.so
        self._build_codex(root)              # links the bridge from the shim step → must follow it
        self._build_kimi(root)               # independent: kimi/native builds its own bridge copy
        self._bundle_artifacts(root)         # copy (+strip codex) into the wheel under py* pkgs

    def _build_antigravity_shim(self, root):
        if not os.path.isdir(os.path.join(root, "antigravity")):
            return  # antigravity/ absent (degenerate checkout with the source tree missing)
        # CMake + Ninja (both pixi host-dependencies) run the whole chain: configure fetches agy
        # (sha512-verified) + the frida-gum devkit into vendor/, then the build generates
        # symbols_gen.h from the committed symbols.json and compiles wirecap_bridge + the shim.
        # All idempotent; picks up conda's g++/python3-config from the env ($CXX / $CONDA_PREFIX).
        build = os.path.join("antigravity", "build")
        subprocess.run(["cmake", "-S", "antigravity", "-B", build, "-G", "Ninja"], cwd=root, check=True)
        subprocess.run(["cmake", "--build", build], cwd=root, check=True)
        sys.stderr.write("[setup] antigravity.so + wirecap_bridge built\n")

    def _build_codex(self, root):
        codex_rs = os.path.join(root, "codex", "vendor", "codex-rs")
        if not os.path.isdir(codex_rs):
            return  # codex-rs absent (degenerate checkout with the source tree missing)
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

    def _build_kimi(self, root):
        vendor = os.path.join(root, "kimi", "vendor", "kimi-code")
        if not os.path.isdir(vendor):
            return  # kimi-code absent (degenerate checkout with the source tree missing)
        # 1) The CLI bundle. pnpm is pinned by the vendored packageManager field and run via
        #    `npx -y pnpm@<pin>` (npm/npx ship with the conda nodejs host-dep; conda-forge has no
        #    pnpm 10). The vis-web asset is stubbed — it only feeds `kimi vis` — so the
        #    vite/tailwind prebuild is skipped; invoking tsdown directly (not `pnpm run build`)
        #    also skips the darwin/win32 native-asset copy and the dist-web check, neither of
        #    which the instrumented Linux CLI needs. tsdown produces a single self-sufficient
        #    dist/main.mjs (the minidb/search workers are SEA-only — no-ops in the ESM bundle).
        #    Idempotent: pnpm/tsdown no-op when nothing changed.
        stub = os.path.join(vendor, "apps", "kimi-code", "src", "generated", "vis-web-asset.ts")
        if not os.path.exists(stub):
            os.makedirs(os.path.dirname(stub), exist_ok=True)
            with open(stub, "w") as f:
                f.write("export const VIS_WEB_GZIP_B64 = '';\n")
        with open(os.path.join(vendor, "package.json")) as f:
            pin = json.load(f).get("packageManager", "pnpm@10").split("+")[0]
        pnpm = ["npx", "-y", pin]
        app = os.path.join(vendor, "apps", "kimi-code")
        subprocess.run([*pnpm, "install", "--frozen-lockfile"], cwd=vendor, check=True)
        subprocess.run([*pnpm, "exec", "tsdown"], cwd=app, check=True)
        # 2) The N-API addon hosting the wirecap bridge (embedded CPython). Builds its own copy
        #    of libwirecap_bridge.a (kimi/native adds ../../wirecap/native as a subdirectory), so
        #    it does not depend on the antigravity build tree.
        build = os.path.join("kimi", "native", "build")
        subprocess.run(["cmake", "-S", os.path.join("kimi", "native"), "-B", build, "-G", "Ninja"],
                       cwd=root, check=True)
        subprocess.run(["cmake", "--build", build], cwd=root, check=True)
        sys.stderr.write("[setup] kimi-code bundle + wirecap_node.node built\n")

    def _bundle_artifacts(self, root):
        # Make the wheel self-contained: copy the native artifacts from their sibling vendor/
        # build dirs into the package tree under build_lib, so bdist_wheel zips them in and the
        # runtime resolver (_vendored) finds them at pyagy/vendor/… and pycodex/vendor/…. The
        # existence check is defensive — after the required builds above the artifacts are present;
        # it only no-ops on a degenerate sourceless checkout that skipped the builds entirely.
        jobs = [
            (os.path.join(root, "antigravity", "vendor", "antigravity.so"), "pyagy", "antigravity.so", False),
            (os.path.join(root, "antigravity", "vendor", "agy"),            "pyagy", "agy",            False),  # build-id coupled to the shim — never strip
            (os.path.join(root, "codex", "vendor", "codex-rs", "target", "release", "codex"), "pycodex", "codex", True),  # ~72% debuginfo — strip for the wheel
            (os.path.join(root, "kimi", "vendor", "kimi-code", "apps", "kimi-code", "dist", "main.mjs"), "pykimi", "main.mjs", False),
            (os.path.join(root, "kimi", "native", "build", "wirecap_node.node"), "pykimi", "wirecap_node.node", False),
        ]
        for src, pkg, name, do_strip in jobs:
            if not os.path.isfile(src):
                sys.stderr.write(f"[setup] bundle: {src} absent — not bundling (pure-Python wheel)\n")
                continue
            dest_dir = os.path.join(self.build_lib, pkg, "vendor")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, name)
            shutil.copy2(src, dest)   # preserves the executable bit
            if do_strip:
                strip = os.environ.get("STRIP") or "strip"
                try:
                    subprocess.run([strip, "--strip-debug", dest], check=True)
                except (OSError, subprocess.CalledProcessError) as exc:
                    sys.stderr.write(f"[setup] bundle: strip of {dest} failed ({exc}); shipping unstripped\n")
            sys.stderr.write(f"[setup] bundled {pkg}/vendor/{name}\n")


setup(distclass=BinaryDistribution, cmdclass={"build_py": BuildPyNative})
