fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        println!("cargo:rustc-link-arg=-ObjC");
    }
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("linux") {
        // Relocatable RUNPATH so the packaged codex finds the conda libpython/Boost with no
        // LD_LIBRARY_PATH. In the self-contained wheel codex lives at
        // <prefix>/lib/pythonX.Y/site-packages/pycodex/vendor/codex, so $ORIGIN/../../../.. is the
        // env's lib/, wherever the wheel is installed. ($CONDA_PREFIX/lib — for running codex in
        // place from the source/editable checkout — is injected by the conda compiler wrapper and
        // by the wirecap crate's build.rs.) $ORIGIN reaches DT_RUNPATH literally: no shell in the
        // cargo->rustc->cc->ld arg chain. This is the binary crate, so the arg hits the codex link
        // (a library dep's rustc-link-arg would not propagate to the final binary).
        println!("cargo:rustc-link-arg=-Wl,--enable-new-dtags,-rpath,$ORIGIN/../../../..");
    }
}
