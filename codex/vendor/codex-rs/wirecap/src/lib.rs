//! codex-wirecap — the Rust FFI shim onto the shared native bridge (`wirecap/native`,
//! `libwirecap_bridge.a`), which embeds CPython on a worker thread and calls the Python
//! `dispatch(kind, stream_id, data)` on the module named by `WIRE_MODULE`.
//!
//! Capture calls sit at the HTTP boundary: the `/v1/responses` request and each streamed
//! `ResponsesStreamEvent` share a [`ResponseCapture`] for exact request correlation.
//!
//! Fully gated: [`start`] no-ops unless `WIRE_ENABLE` is set, and [`emit`] no-ops until the
//! bridge reports ready — so a normal `codex` run (outside pycodex) never spins up libpython.
//! All calls are ASYNC (the bridge copies + enqueues), so they are safe to call from tokio
//! worker threads: they never take the Python GIL on the caller's thread.

use std::ffi::CStr;
use std::ffi::c_char;
use std::ffi::c_int;
use std::sync::Once;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;

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

/// Drain accepted captures and join the bridge worker at the end of `main()`.
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
pub fn emit(kind: &'static CStr, stream_id: u64, data: &[u8]) {
    // SAFETY: the bridge copies data before returning, but borrows kind until dispatch.
    // The static lifetime keeps that queued pointer valid. No GIL is taken here.
    unsafe {
        if wire_ready() == 0 {
            return;
        }
        wire_emit_async(kind.as_ptr(), stream_id, data.as_ptr(), data.len());
    }
}

/// Capture one transport request and its response, including cancellation/error cleanup.
pub struct ResponseCapture {
    id: u64,
}

impl ResponseCapture {
    pub fn new(body: &[u8]) -> Self {
        let id = TURN.fetch_add(1, Ordering::Relaxed) + 1;
        emit(c"codex_request", id, body);
        Self { id }
    }

    pub fn event(&self, body: &[u8]) {
        emit(c"codex_event", self.id, body);
    }
}

impl Drop for ResponseCapture {
    fn drop(&mut self) {
        emit(c"codex_end", self.id, &[]);
    }
}
