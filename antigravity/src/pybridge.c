/* pybridge.c — embedded CPython on a dedicated 16 MB-stack worker thread. */
#define _GNU_SOURCE
#define PY_SSIZE_T_CLEAN   /* required for "#" formats (y#) on Python 3.10+ */
#include "pybridge.h"

#include <Python.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PYLOG(...) do { fprintf(stderr, "[antigravity/py] " __VA_ARGS__); fputc('\n', stderr); } while (0)

typedef struct job {
    struct job *next;
    const char *kind;      /* static literal, not owned */
    uint64_t    stream_id;
    uint8_t    *data;      /* owned by worker; freed after dispatch */
    size_t      len;
    agy_mode_t  mode;
    int         on_heap;   /* 1 => worker frees the job; 0 => stack job (SYNC) */

    /* SYNC completion + result */
    pthread_mutex_t done_mu;
    pthread_cond_t  done_cv;
    int         done;
    int         verdict;
    uint8_t    *out_data;
    size_t      out_len;
} job_t;

static pthread_t       g_worker;
static pthread_mutex_t g_qmu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_qcv = PTHREAD_COND_INITIALIZER;
static job_t          *g_head, *g_tail;
static int             g_stop;

static pthread_mutex_t g_startmu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_startcv = PTHREAD_COND_INITIALIZER;
static int             g_ready = -1;   /* -1 starting, 0 failed, 1 ready */

static size_t          g_maxcopy = 1u << 20;
static PyObject       *g_dispatch;     /* pyagy.agy_process.dispatch (borrowed on worker thread) */

int agy_py_ready(void) { return g_ready == 1; }

static void enqueue(job_t *j)
{
    pthread_mutex_lock(&g_qmu);
    j->next = NULL;
    if (g_tail) g_tail->next = j; else g_head = j;
    g_tail = j;
    pthread_cond_signal(&g_qcv);
    pthread_mutex_unlock(&g_qmu);
}

/* Called on the worker thread, holding the GIL. */
static void run_dispatch(job_t *j)
{
    if (!g_dispatch) return;
    /* Keep the (ptr,len) pair consistent: if there's no buffer, length is 0 so the
     * "y#" format never reads past the empty literal. */
    const char *buf = j->data ? (const char *)j->data : "";
    Py_ssize_t dlen = j->data ? (Py_ssize_t)j->len : 0;
    PyObject *r = PyObject_CallFunction(g_dispatch, "sKy#",
                                        j->kind, (unsigned long long)j->stream_id,
                                        buf, dlen);
    if (!r) {
        PyErr_Print();
        return;
    }
    if (j->mode == AGY_SYNC && PyBytes_Check(r)) {
        Py_ssize_t n = PyBytes_GET_SIZE(r);
        uint8_t *out = malloc(n ? (size_t)n : 1);
        if (out) {
            memcpy(out, PyBytes_AS_STRING(r), (size_t)n);
            j->out_data = out;
            j->out_len = (size_t)n;
            j->verdict = 1;
        }
    }
    Py_DECREF(r);
}

static void *worker_main(void *arg)
{
    (void)arg;
    const char *modname = getenv("AGY_PROC_MODULE");
    if (!modname || !*modname) modname = "pyagy.agy_process";
    const char *pypath = getenv("AGY_PROC_PYTHONPATH");
    const char *mc = getenv("AGY_PROC_MAXCOPY");
    if (mc && *mc) { long v = strtol(mc, NULL, 0); if (v > 0) g_maxcopy = (size_t)v; }

    Py_InitializeEx(0);  /* no signal handlers — leave those to Go */

    if (pypath && *pypath) {
        PyObject *sys = PyImport_ImportModule("sys");
        PyObject *path = sys ? PyObject_GetAttrString(sys, "path") : NULL;
        if (path) {
            PyObject *p = PyUnicode_FromString(pypath);
            PyList_Insert(path, 0, p);
            Py_XDECREF(p);
        }
        Py_XDECREF(path); Py_XDECREF(sys);
    }

    PyObject *mod = PyImport_ImportModule(modname);
    if (!mod) {
        PYLOG("failed to import '%s':", modname);
        PyErr_Print();
    } else {
        g_dispatch = PyObject_GetAttrString(mod, "dispatch");
        if (!g_dispatch) { PYLOG("module '%s' has no dispatch()", modname); PyErr_Clear(); }
    }

    int ok = (g_dispatch != NULL);
    PyThreadState *saved = PyEval_SaveThread();  /* release GIL while idle */

    pthread_mutex_lock(&g_startmu);
    g_ready = ok ? 1 : 0;
    pthread_cond_broadcast(&g_startcv);
    pthread_mutex_unlock(&g_startmu);
    if (!ok) { PyEval_RestoreThread(saved); return NULL; }
    PYLOG("worker ready (module=%s, maxcopy=%zu)", modname, g_maxcopy);

    for (;;) {
        pthread_mutex_lock(&g_qmu);
        while (!g_head && !g_stop) pthread_cond_wait(&g_qcv, &g_qmu);
        if (g_stop && !g_head) { pthread_mutex_unlock(&g_qmu); break; }
        job_t *j = g_head;
        g_head = j->next;
        if (!g_head) g_tail = NULL;
        pthread_mutex_unlock(&g_qmu);

        PyGILState_STATE gil = PyGILState_Ensure();
        run_dispatch(j);
        PyGILState_Release(gil);

        free(j->data);
        if (j->mode == AGY_SYNC) {
            pthread_mutex_lock(&j->done_mu);
            j->done = 1;
            pthread_cond_signal(&j->done_cv);
            pthread_mutex_unlock(&j->done_mu);   /* emitter owns the stack job */
        } else if (j->on_heap) {
            free(j);
        }
    }
    (void)saved;
    return NULL;
}

int agy_py_start(void)
{
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 16u * 1024 * 1024);  /* big stack for libpython */
    int rc = pthread_create(&g_worker, &attr, worker_main, NULL);
    pthread_attr_destroy(&attr);
    if (rc != 0) { PYLOG("pthread_create failed: %d", rc); g_ready = 0; return -1; }

    pthread_mutex_lock(&g_startmu);
    while (g_ready == -1) pthread_cond_wait(&g_startcv, &g_startmu);
    pthread_mutex_unlock(&g_startmu);
    return g_ready == 1 ? 0 : -1;
}

static uint8_t *copy_capped(const uint8_t *src, size_t len, size_t *out_len)
{
    size_t n = len > g_maxcopy ? g_maxcopy : len;
    if (n == 0) { *out_len = 0; return NULL; }
    uint8_t *p = malloc(n);
    if (!p) { *out_len = 0; return NULL; }   /* alloc failed → keep data/len consistent */
    if (src) memcpy(p, src, n);
    *out_len = n;
    return p;
}

void agy_py_emit(agy_event_t *ev)
{
    if (g_ready != 1) return;

    if (ev->mode == AGY_ASYNC) {
        job_t *j = calloc(1, sizeof(*j));
        if (!j) return;
        j->on_heap = 1;
        j->kind = ev->kind;
        j->stream_id = ev->stream_id;
        j->mode = AGY_ASYNC;
        j->data = copy_capped(ev->data, ev->len, &j->len);
        enqueue(j);
        return;
    }

    /* SYNC: stack job, block until the worker fills the verdict. */
    job_t j;
    memset(&j, 0, sizeof(j));
    pthread_mutex_init(&j.done_mu, NULL);
    pthread_cond_init(&j.done_cv, NULL);
    j.kind = ev->kind;
    j.stream_id = ev->stream_id;
    j.mode = AGY_SYNC;
    j.data = copy_capped(ev->data, ev->len, &j.len);

    enqueue(&j);
    pthread_mutex_lock(&j.done_mu);
    while (!j.done) pthread_cond_wait(&j.done_cv, &j.done_mu);
    pthread_mutex_unlock(&j.done_mu);
    pthread_mutex_destroy(&j.done_mu);
    pthread_cond_destroy(&j.done_cv);

    ev->verdict = j.verdict;
    ev->out_data = j.out_data;   /* caller frees via agy_py_free */
    ev->out_len = j.out_len;
}

void agy_py_free(agy_event_t *ev)
{
    if (ev->out_data) { free(ev->out_data); ev->out_data = NULL; ev->out_len = 0; ev->verdict = 0; }
}

void agy_py_stop(void)
{
    pthread_mutex_lock(&g_qmu);
    g_stop = 1;
    pthread_cond_signal(&g_qcv);
    pthread_mutex_unlock(&g_qmu);
}
