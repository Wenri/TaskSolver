//! codex-wirecap — the Rust FFI shim onto the shared native bridge (`wirecap/native`,
//! `libwirecap_bridge.a`), which embeds CPython on a worker thread and calls the Python
//! `dispatch(kind, stream_id, data)` on the module named by `WIRE_MODULE`.
//!
//! This is the codex analog of the antigravity shim's `wire_emit` call sites: instead of
//! LD_PRELOAD/gum hooking a closed binary, the vendored codex source calls [`emit`] at its
//! HTTP boundary (the `/v1/responses` request + each streamed `ResponsesStreamEvent`).
//!
//! Fully gated: [`start`] no-ops unless `WIRE_ENABLE` is set, and [`emit`] no-ops until the
//! bridge reports ready — so a normal `codex` run (outside pycodex) never spins up libpython.
//! All calls are ASYNC (the bridge copies + enqueues), so they are safe to call from tokio
//! worker threads: they never take the Python GIL on the caller's thread.

use std::ffi::{c_char, c_int, CStr};
use std::sync::Once;
use std::sync::atomic::{AtomicU64, Ordering};

unsafe extern "C" {
    fn wire_start() -> c_int;
    fn wire_ready() -> c_int;
    fn wire_emit_async(kind: *const c_char, stream_id: u64, data: *const u8, len: usize);
    fn wire_shutdown();
}

static STARTED: Once = Once::new();
static TURN: AtomicU64 = AtomicU64::new(0);

/// Start the embedded-CPython bridge once, iff `WIRE_ENABLE` is set. Call at the very top of
/// `main()`, before the tokio runtime — CPython initializes on the bridge's own worker thread.
pub fn start() {
    if std::env::var_os("WIRE_ENABLE").is_none() {
        return;
    }
    STARTED.call_once(|| unsafe {
        let _ = wire_start();
    });
}

/// Cooperatively stop + join the bridge worker. Call once at the very end of `main()`, mirroring
/// [`start`] — this is the deterministic teardown the bridge documents (agy reaches it from its
/// os.Exit hook, since Go's os.Exit bypasses libc exit entirely).
///
/// Without it the bridge is only torn down by `~PyBridge` at libc exit, which its own comments call
/// a fallback and warn is a use-after-destruction hazard if another thread is still inside
/// `bridge()` — a live risk for codex, which is the more threaded of the two consumers (tokio).
///
/// No-op unless [`start`] actually ran, so a plain `codex` run (no WIRE_ENABLE) never touches
/// libpython. NOTE this does not cover the paths that call `std::process::exit` (the interactive
/// TUI's fatal-error exit, and `codex debug`'s handle_exit_status); `codex exec` — the path pycodex
/// drives — returns from main normally and does reach here.
pub fn shutdown() {
    if !STARTED.is_completed() {
        return;
    }
    unsafe { wire_shutdown() };
}

/// Emit a capture event (ASYNC). No-op until the bridge is ready.
pub fn emit(kind: &CStr, stream_id: u64, data: &[u8]) {
    // SAFETY: wire_ready/wire_emit_async are the bridge's C ABI; `data`/`kind` outlive the call
    // (the bridge copies the bytes before returning). ASYNC → no GIL on this thread.
    unsafe {
        if wire_ready() == 0 {
            return;
        }
        wire_emit_async(kind.as_ptr(), stream_id, data.as_ptr(), data.len());
    }
}

/// Emit the model REQUEST body (`/v1/responses` JSON), starting a new turn id. Returns that id
/// so the response events of the same turn can be tagged with it.
pub fn emit_request(body: &[u8]) -> u64 {
    let id = TURN.fetch_add(1, Ordering::Relaxed) + 1;
    emit(c"codex_request", id, body);
    id
}

/// Emit one streamed response event (`ResponsesStreamEvent` JSON), tagged with the current turn.
pub fn emit_event(body: &[u8]) {
    emit(c"codex_event", TURN.load(Ordering::Relaxed), body);
}
