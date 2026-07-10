/* wirecap.h — the shared native bridge: embed CPython on a dedicated large-stack worker
 * thread and dispatch capture events to it. Linked by every instrumentation front-end
 * (the antigravity LD_PRELOAD shim; the patched codex build), which fills a wire_event_t
 * and calls wire_emit(); all Python runs on the worker thread (16 MB stack, single
 * PyThreadState). See antigravity/README "Why a dedicated worker thread".
 *
 * The API keeps C linkage so C, C++, and Rust (extern "C") front-ends all link the same
 * unmangled symbols.
 */
#ifndef WIRECAP_H
#define WIRECAP_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { WIRE_ASYNC = 0, WIRE_SYNC = 1 } wire_mode_t;

typedef struct {
    const char *kind;      /* event tag → dispatch(kind, ...) (e.g. "tls_write", "resp_chunk") */
    uint64_t    stream_id; /* conn/transport/turn id, for per-stream reassembly */
    const uint8_t *data;   /* borrowed; valid for the call. SYNC: the bridge may REWRITE it in place
                            * (equal-or-shorter) with the worker's replacement — see verdict below. */
    size_t      len;
    wire_mode_t mode;

    /* outputs, SYNC only: verdict=1 means the bridge rewrote `data` in place with an equal-or-shorter
     * replacement; out_len is its new length (honor it, e.g. shrink the slice the callee sees). */
    int         verdict;
    size_t      out_len;
} wire_event_t;

/* Start the interpreter + worker thread. Reads env:
 *   WIRE_MODULE     python module to import (default "pyagy.agy_process"); resolved from the
 *                   embedded interpreter's own site-packages (site runs; PYTHONHOME selects the env)
 *   WIRE_MAXCOPY    max bytes copied per event (default 1<<20)
 * Returns 0 on success. Never aborts the host on failure. */
int  wire_start(void);

/* Emit an event. ASYNC: copies data, enqueues, returns immediately.
 * SYNC: enqueues and blocks until the worker returns a verdict. */
void wire_emit(wire_event_t *ev);

/* Scalar-arg convenience for the common ASYNC capture path — trivial to call over FFI
 * (e.g. from Rust) without constructing a wire_event_t. Never returns a verdict. */
void wire_emit_async(const char *kind, uint64_t stream_id, const uint8_t *data, size_t len);

/* Reset the SYNC output fields after honoring the verdict. The replacement (if any) was written
 * into ev->data in place by emit — there's no separate buffer to free. */
void wire_free(wire_event_t *ev);

int  wire_ready(void);

/* Cooperatively stop + join the worker (idempotent). Called from the front-end's teardown
 * (e.g. the shim's os.Exit hook) after the end-of-capture marker is emitted. */
void wire_shutdown(void);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif
