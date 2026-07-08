//! Link the codex binary against the shared native bridge (`libwirecap_bridge.a`, built by the
//! antigravity CMake as a byproduct of `pixi run build-shim`) and its runtime deps (the conda
//! libpython + Boost.Python/Thread + libstdc++, versions derived from $CONDA_PREFIX's python3).
//! These directives propagate from this crate to the final `codex` binary. RPATH
//! `$CONDA_PREFIX/lib` resolves the .so's at run time — the same mechanism the antigravity
//! shim uses.
//!
//! Requires the pixi env (CONDA_PREFIX set) and that the bridge lib has been built. Override the
//! bridge dir with WIRECAP_BRIDGE_DIR if it lives elsewhere.

use std::path::PathBuf;

fn main() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    // manifest = <repo>/codex/vendor/codex-rs/wirecap → repo root is four levels up.
    let repo = manifest.join("../../../..").canonicalize().unwrap();
    let bridge_dir = std::env::var("WIRECAP_BRIDGE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| repo.join("antigravity/build/wirecap_bridge"));
    let conda = PathBuf::from(
        std::env::var("CONDA_PREFIX").expect("CONDA_PREFIX not set — build inside the pixi env"),
    );
    let conda_lib = conda.join("lib");

    // libpython / Boost.Python names carry the python version (python3.13, boost_python313, …) —
    // derive it from the env's python3 so the link matches the libpython the bridge embedded.
    let out = std::process::Command::new(conda.join("bin/python3"))
        .args(["-c", "import sys;print('%d.%d' % sys.version_info[:2])"])
        .output()
        .expect("failed to run $CONDA_PREFIX/bin/python3 to derive the python version");
    let py_ver = String::from_utf8(out.stdout).unwrap().trim().to_string();
    assert!(!py_ver.is_empty(), "empty python version from $CONDA_PREFIX/bin/python3");
    let py_tag = py_ver.replace('.', "");

    // The static bridge FIRST, then the dylibs that satisfy its undefined Py*/boost/C++ symbols.
    println!("cargo:rustc-link-search=native={}", bridge_dir.display());
    println!("cargo:rustc-link-lib=static=wirecap_bridge");
    println!("cargo:rustc-link-search=native={}", conda_lib.display());
    println!("cargo:rustc-link-lib=dylib=python{py_ver}");
    println!("cargo:rustc-link-lib=dylib=boost_python{py_tag}");
    println!("cargo:rustc-link-lib=dylib=boost_thread");
    println!("cargo:rustc-link-lib=dylib=stdc++");
    println!("cargo:rustc-link-arg=-Wl,-rpath,{}", conda_lib.display());

    // Rebuild if the bridge lib is regenerated.
    println!("cargo:rerun-if-changed={}", bridge_dir.join("libwirecap_bridge.a").display());
    println!("cargo:rerun-if-env-changed=WIRECAP_BRIDGE_DIR");
    println!("cargo:rerun-if-env-changed=CONDA_PREFIX");
}
