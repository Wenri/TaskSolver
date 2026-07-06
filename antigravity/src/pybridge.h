/* pybridge.h — embed CPython on a dedicated large-stack worker thread and
 * dispatch hook events to it. See README "Why a dedicated worker thread".
 *
 * The gum hooks run on tiny goroutine stacks, so they must NOT call libpython
 * directly. They fill an agy_event_t and call agy_py_emit(); all Python runs on
 * the worker thread (16 MB stack, single PyThreadState).
 */
#ifndef AGY_PYBRIDGE_H
#define AGY_PYBRIDGE_H

#include <stddef.h>
#include <stdint.h>

/* All shim TUs are C++ now, but the whole hook API keeps C linkage: pybridge.cpp defines
 * these and the antigravity/gomod/cgotrampoline TUs call them, so unmangled names keep the
 * cross-TU wiring (and the exported symbols) byte-identical regardless of compiler. */
#ifdef __cplusplus
extern "C" {
#endif

typedef enum { AGY_ASYNC = 0, AGY_SYNC = 1 } agy_mode_t;

typedef struct {
    const char *kind;      /* "tls_write" | "tls_read" | "http_rt" | "dns" | "smoke" */
    uint64_t    stream_id; /* conn/transport pointer, for per-stream reassembly */
    const uint8_t *data;   /* borrowed; valid only for the duration of the call */
    size_t      len;
    agy_mode_t  mode;

    /* outputs, SYNC only: verdict=1 means use out_data/out_len. The buffer lives in a
     * thread-local in the bridge; borrowed, valid until the emitting hook returns. */
    int         verdict;
    uint8_t    *out_data;
    size_t      out_len;
} agy_event_t;

/* Start the interpreter + worker thread. Reads env:
 *   AGY_PROC_MODULE     python module to import (default "pyagy.agy_process")
 *   AGY_PROC_PYTHONPATH prepended to sys.path
 *   AGY_PROC_MAXCOPY    max bytes copied per event (default 1<<20)
 * Returns 0 on success. Never aborts the host on failure. */
int  agy_py_start(void);

/* Emit an event. ASYNC: copies data, enqueues, returns immediately.
 * SYNC: enqueues and blocks until the worker returns a verdict. */
void agy_py_emit(agy_event_t *ev);

/* Reset the SYNC outputs after applying ev->out_data (the buffer is thread-local in
 * the bridge, not owned by the caller — nothing is freed). */
void agy_py_free(agy_event_t *ev);

int  agy_py_ready(void);

/* Cooperatively stop + join the worker (idempotent). Called from the os.Exit hook — the one
 * teardown callback that fires under agy's Go exit — after the end-of-capture marker is emitted. */
void agy_py_shutdown(void);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif
