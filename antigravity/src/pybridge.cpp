/* pybridge.cpp — embedded CPython on a dedicated 16 MB-stack worker thread (Boost.Thread).
 *
 * C++23 TU (the rest of the shim is C). Boost.Thread is used over std::thread because only
 * boost::thread::attributes can set the worker's 16 MB stack — libpython's C stack is deep and
 * std::thread/jthread expose no stack-size API. The exported API has C linkage (see pybridge.h)
 * so the C TUs (antigravity.c, gomod.c, cgotrampoline.c) link against it.
 *
 * Boost.Python (bp::) handles the Python object/call/refcount layer (import, attrs, calling
 * dispatch, extracting the result) — RAII refcounting + error_already_set instead of manual
 * PyObject* / Py_DECREF / NULL checks. It does NOT wrap interpreter bootstrap or the GIL (no
 * gil.hpp in Boost 1.91), so Py_InitializeEx, PyEval_SaveThread, and PyGILState_Ensure/Release
 * stay raw C-API. g_dispatch stays a raw PyObject* on purpose (a static bp::object would run an
 * exit-time Py_DECREF without the GIL in this never-Py_Finalize'd interp). */
#define PY_SSIZE_T_CLEAN   /* required for "#" formats (y#) on Python 3.10+ */
#include <boost/python.hpp>   /* pulls in Python.h first, with the right guards */
#include "pybridge.h"

#include <boost/thread.hpp>
#include <boost/thread/condition_variable.hpp>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>

namespace bp = boost::python;

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

/* Called on the worker thread, holding the GIL. Every bp::object below is created AND destroyed
 * inside this GIL window — none escapes to a GIL-less context. */
static void run_dispatch(job *j)
{
    if (!g_dispatch) return;
    try {
        /* The "y#" payload must be real Python bytes — bp::str/std::string would build PyUnicode
         * (str) and corrupt binary data. Build bytes via the C-API and adopt into a handle (throws
         * error_already_set on NULL). Empty buffer → 0-length bytes, consistent (ptr,len). */
        const char *buf = j->data ? (const char *)j->data : "";
        Py_ssize_t  dlen = j->data ? (Py_ssize_t)j->len : 0;
        bp::object arg(bp::handle<>(PyBytes_FromStringAndSize(buf, dlen)));

        /* dispatch(kind: str, stream_id: int, data: bytes) — the old "sKy#" call. bp::call takes
         * the raw PyObject* callable directly; the result is a new-ref bp::object (RAII decref). */
        bp::object r = bp::call<bp::object>(g_dispatch,
                                            bp::str(j->kind),
                                            (unsigned long long)j->stream_id,
                                            arg);

        if (j->mode == AGY_SYNC && !r.is_none() && PyBytes_Check(r.ptr())) {
            Py_ssize_t n = PyBytes_GET_SIZE(r.ptr());
            uint8_t *out = (uint8_t *)malloc(n ? (size_t)n : 1);
            if (out) {
                memcpy(out, PyBytes_AS_STRING(r.ptr()), (size_t)n);
                j->out_data = out;
                j->out_len = (size_t)n;
                j->verdict = 1;
            }
        }
        /* arg and r destruct here (GIL held) → automatic Py_DECREF. */
    }
    catch (const bp::error_already_set &) {
        /* error_already_set carries no message — the detail is in CPython's error indicator;
         * inspect it here before clearing (PyErr_Print also clears). */
        if (PyErr_Occurred()) PyErr_Print();
        PyErr_Clear();
    }
    catch (...) {
        /* Backstop: run_dispatch runs in the boost::thread loop with the GIL held; an escaping
         * C++ exception would std::terminate the process (and leak the GIL). Never unwind out. */
        PyErr_Clear();
    }
}

static void worker_main()
{
    const char *modname = getenv("AGY_PROC_MODULE");
    if (!modname || !*modname) modname = "pyagy.agy_process";
    const char *pypath = getenv("AGY_PROC_PYTHONPATH");
    const char *mc = getenv("AGY_PROC_MAXCOPY");
    if (mc && *mc) { long v = strtol(mc, NULL, 0); if (v > 0) g_maxcopy = (size_t)v; }

    Py_InitializeEx(0);  /* raw C-API bootstrap — Boost.Python has no embedding entry point */

    /* GIL is held from Py_InitializeEx until PyEval_SaveThread() below, so these bp::objects are
     * fine; all of them (sys/mod/disp/arg) destruct before the GIL drops. */
    try {
        bp::object sys = bp::import(bp::str("sys"));
        sys.attr("is_agy_shim") = true;                 /* → Py_True */
        if (pypath && *pypath)
            sys.attr("path").attr("insert")(0, bp::str(pypath));   /* sys.path.insert(0, pypath) */

        bp::object mod = bp::import(bp::str(modname));
        bp::object disp = mod.attr("dispatch");         /* transient new-ref, destructs under the GIL */
        /* Pin ONE strong raw ref forever. g_dispatch MUST stay a raw PyObject* (not a static
         * bp::object) — a static bp::object's dtor would Py_DECREF at process exit, GIL-less, in a
         * never-Py_Finalize'd interp (UB). This mirrors Boost's own scope.hpp forever-ref idiom. */
        g_dispatch = bp::incref(disp.ptr());
    }
    catch (const bp::error_already_set &) {
        PYLOG("failed to init module '%s':", modname);
        if (PyErr_Occurred()) PyErr_Print();
        PyErr_Clear();
        g_dispatch = nullptr;                            /* → ok=false, graceful degrade */
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
