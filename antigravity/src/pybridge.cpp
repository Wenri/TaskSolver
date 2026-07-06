/* pybridge.cpp — embedded CPython on a dedicated 16 MB-stack worker thread (Boost.Thread).
 *
 * C++23 TU (the rest of the shim is C). Boost.Thread is used over std::thread because only
 * boost::thread::attributes can set the worker's 16 MB stack — libpython's C stack is deep and
 * std::thread/jthread expose no stack-size API. The exported API has C linkage (see pybridge.h)
 * so the C TUs (antigravity.c, gomod.c, cgotrampoline.c) link against it. */
#define PY_SSIZE_T_CLEAN   /* required for "#" formats (y#) on Python 3.10+ */
#include "pybridge.h"

#include <Python.h>

#include <boost/thread.hpp>
#include <boost/thread/condition_variable.hpp>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>

#define PYLOG(...) do { fprintf(stderr, "[antigravity/py] " __VA_ARGS__); fputc('\n', stderr); } while (0)

struct job {
    job        *next = nullptr;
    const char *kind = nullptr;    /* static literal, not owned */
    uint64_t    stream_id = 0;
    uint8_t    *data = nullptr;    /* owned by worker; freed after dispatch */
    size_t      len = 0;
    agy_mode_t  mode = AGY_ASYNC;
    bool        on_heap = false;   /* true => worker deletes the job; false => stack job (SYNC) */

    /* SYNC completion + result */
    boost::mutex              done_mu;
    boost::condition_variable done_cv;
    bool        done = false;
    int         verdict = 0;
    uint8_t    *out_data = nullptr;
    size_t      out_len = 0;
};

static boost::thread             g_worker;
static boost::mutex              g_qmu;
static boost::condition_variable g_qcv;
static job                      *g_head, *g_tail;

static boost::mutex              g_startmu;
static boost::condition_variable g_startcv;
static int                       g_ready = -1;   /* -1 starting, 0 failed, 1 ready */

static size_t          g_maxcopy = 1u << 20;
static PyObject       *g_dispatch;     /* pyagy.agy_process.dispatch (borrowed on worker thread) */

extern "C" int agy_py_ready(void) { return g_ready == 1; }

static void enqueue(job *j)
{
    boost::unique_lock<boost::mutex> lk(g_qmu);
    j->next = nullptr;
    if (g_tail) g_tail->next = j; else g_head = j;
    g_tail = j;
    g_qcv.notify_one();
}

/* Called on the worker thread, holding the GIL. */
static void run_dispatch(job *j)
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
        uint8_t *out = (uint8_t *)malloc(n ? (size_t)n : 1);
        if (out) {
            memcpy(out, PyBytes_AS_STRING(r), (size_t)n);
            j->out_data = out;
            j->out_len = (size_t)n;
            j->verdict = 1;
        }
    }
    Py_DECREF(r);
}

static void worker_main()
{
    const char *modname = getenv("AGY_PROC_MODULE");
    if (!modname || !*modname) modname = "pyagy.agy_process";
    const char *pypath = getenv("AGY_PROC_PYTHONPATH");
    const char *mc = getenv("AGY_PROC_MAXCOPY");
    if (mc && *mc) { long v = strtol(mc, NULL, 0); if (v > 0) g_maxcopy = (size_t)v; }

    Py_InitializeEx(0);  /* no signal handlers — leave those to Go */

    PyObject *sys = PyImport_ImportModule("sys");
    if (sys) {
        PyObject_SetAttrString(sys, "is_agy_shim", Py_True);
        if (pypath && *pypath) {
            PyObject *path = PyObject_GetAttrString(sys, "path");
            if (path) {
                PyObject *p = PyUnicode_FromString(pypath);
                PyList_Insert(path, 0, p);
                Py_XDECREF(p);
                Py_DECREF(path);
            }
        }
        Py_DECREF(sys);
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

    {
        boost::unique_lock<boost::mutex> lk(g_startmu);
        g_ready = ok ? 1 : 0;
        g_startcv.notify_all();
    }
    if (!ok) { PyEval_RestoreThread(saved); return; }
    PYLOG("worker ready (module=%s, maxcopy=%zu)", modname, g_maxcopy);

    for (;;) {
        job *j;
        {
            boost::unique_lock<boost::mutex> lk(g_qmu);
            g_qcv.wait(lk, [] { return g_head != nullptr; });
            j = g_head;
            g_head = j->next;
            if (!g_head) g_tail = nullptr;
        }

        PyGILState_STATE gil = PyGILState_Ensure();
        run_dispatch(j);
        PyGILState_Release(gil);

        free(j->data);
        if (j->mode == AGY_SYNC) {
            boost::unique_lock<boost::mutex> lk(j->done_mu);
            j->done = true;
            j->done_cv.notify_one();   /* emitter owns the stack job */
        } else if (j->on_heap) {
            delete j;
        }
    }
    (void)saved;
}

extern "C" int agy_py_start(void)
{
    try {
        boost::thread::attributes attrs;
        attrs.set_stack_size(16u * 1024 * 1024);  /* big stack for libpython */
        g_worker = boost::thread(attrs, &worker_main);
        g_worker.detach();  /* fire-and-forget; runs until the process dies (matches pthread) */
    } catch (const std::exception &e) {
        PYLOG("boost::thread create failed: %s", e.what());
        g_ready = 0;
        return -1;
    }

    boost::unique_lock<boost::mutex> lk(g_startmu);
    g_startcv.wait(lk, [] { return g_ready != -1; });
    return g_ready == 1 ? 0 : -1;
}

static uint8_t *copy_capped(const uint8_t *src, size_t len, size_t *out_len)
{
    size_t n = len > g_maxcopy ? g_maxcopy : len;
    if (n == 0) { *out_len = 0; return nullptr; }
    uint8_t *p = (uint8_t *)malloc(n);
    if (!p) { *out_len = 0; return nullptr; }   /* alloc failed → keep data/len consistent */
    if (src) memcpy(p, src, n);
    *out_len = n;
    return p;
}

extern "C" void agy_py_emit(agy_event_t *ev)
{
    if (g_ready != 1) return;

    if (ev->mode == AGY_ASYNC) {
        job *j = new (std::nothrow) job();
        if (!j) return;
        j->on_heap = true;
        j->kind = ev->kind;
        j->stream_id = ev->stream_id;
        j->mode = AGY_ASYNC;
        j->data = copy_capped(ev->data, ev->len, &j->len);
        enqueue(j);
        return;
    }

    /* SYNC: stack job, block until the worker fills the verdict. boost::mutex/condition_variable
     * members are constructed/destroyed with the job — no manual init/destroy. */
    job j;
    j.kind = ev->kind;
    j.stream_id = ev->stream_id;
    j.mode = AGY_SYNC;
    j.data = copy_capped(ev->data, ev->len, &j.len);

    enqueue(&j);
    {
        boost::unique_lock<boost::mutex> lk(j.done_mu);
        j.done_cv.wait(lk, [&] { return j.done; });
    }

    ev->verdict = j.verdict;
    ev->out_data = j.out_data;   /* caller frees via agy_py_free */
    ev->out_len = j.out_len;
}

extern "C" void agy_py_free(agy_event_t *ev)
{
    if (ev->out_data) { free(ev->out_data); ev->out_data = nullptr; ev->out_len = 0; ev->verdict = 0; }
}
