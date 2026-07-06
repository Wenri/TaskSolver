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
 * stay raw C-API.
 *
 * The worker thread, its job queue, and the pinned dispatch callable are encapsulated in the
 * `PyBridge` class; a single leaked-forever instance (`bridge()`) backs the extern "C" entry
 * points. `dispatch_` stays a raw PyObject* on purpose (a bp::object member of a destroyed-at-exit
 * instance would Py_DECREF without the GIL in this never-Py_Finalize'd interp) — which is also why
 * the instance is intentionally never destroyed. */
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

namespace {

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

/* PyBridge — owns the embedded interpreter's worker thread, the job queue, and the pinned
 * dispatch callable. A single leaked instance (see bridge()) lives for the whole process; the
 * extern "C" entry points delegate to it. */
class PyBridge {
public:
    int  start();               /* create the worker thread; block until it signals ready */
    void emit(agy_event_t *ev); /* enqueue an event (ASYNC) or block for a verdict (SYNC) */
    bool ready() const { return ready_ == 1; }
    void shutdown();            /* cooperatively stop + join the worker; idempotent */
    ~PyBridge() { shutdown(); } /* fallback: join the worker BEFORE members destruct, so a dtor on
                                 * the rare libc-exit path can't terminate on a joinable thread or
                                 * tear down an in-use mutex. Dormant on the normal Go-os.Exit path
                                 * (that dtor never runs; the os.Exit hook already called shutdown). */

private:
    void     worker_main();     /* boost::thread body: init the interpreter, then drain the queue */
    bool     enqueue(job *j);   /* false if shutting down (job not queued) */
    void     run_dispatch(job *j);   /* on the worker thread, holding the GIL */
    uint8_t *copy_capped(const uint8_t *src, size_t len, size_t *out_len) const;

    /* One mutex + condvar for everything: the start handshake, the job queue, and shutdown. qcv_
     * carries three signals (ready_ set / job enqueued / stop_ set), each disambiguated by the
     * waiter's predicate — start() waits on `ready_ != -1`, the worker loop on `head_ || stop_`. */
    boost::thread             worker_;
    boost::mutex              qmu_;
    boost::condition_variable qcv_;
    job                      *head_ = nullptr, *tail_ = nullptr;
    bool                      stop_ = false;           /* set by shutdown(); worker drains then exits */
    int                       ready_ = -1;             /* -1 starting, 0 failed, 1 ready (write: qmu_) */
    size_t                    maxcopy_ = 1u << 20;
    PyObject                 *dispatch_ = nullptr;     /* pyagy.agy_process.dispatch; strong raw ref */
};

/* The one instance — a Meyers singleton (thread-safe first-call init, no raw `new` to leak).
 * Cleanup is DETERMINISTIC via shutdown(), invoked from the os.Exit hook (the "exit" branch in
 * cgotrampoline.c) — the one teardown callback that fires under agy's Go os.Exit, which bypasses
 * libc exit / __cxa_atexit (verified), so ~PyBridge does NOT run on the normal path. ~PyBridge is
 * only a fallback for a libc-exit() path, where it join-then-destructs safely. Note the residual
 * corner: if that fallback dtor ever ran while another goroutine was still calling bridge(), it
 * would be a use-after-destruction — but agy never takes that path (it always os.Exits). */
PyBridge &bridge() { static PyBridge b; return b; }

bool PyBridge::enqueue(job *j)
{
    boost::unique_lock<boost::mutex> lk(qmu_);
    if (stop_) return false;      /* shutting down: refuse so no emitter blocks on a dead worker */
    j->next = nullptr;
    if (tail_) tail_->next = j; else head_ = j;
    tail_ = j;
    qcv_.notify_one();
    return true;
}

/* Called on the worker thread, holding the GIL. Every bp::object below is created AND destroyed
 * inside this GIL window — none escapes to a GIL-less context. */
void PyBridge::run_dispatch(job *j)
{
    if (!dispatch_) return;
    try {
        /* The "y#" payload must be real Python bytes — bp::str/std::string would build PyUnicode
         * (str) and corrupt binary data. Build bytes via the C-API and adopt into a handle (throws
         * error_already_set on NULL). Empty buffer → 0-length bytes, consistent (ptr,len). */
        const char *buf = j->data ? (const char *)j->data : "";
        Py_ssize_t  dlen = j->data ? (Py_ssize_t)j->len : 0;
        bp::object arg(bp::handle<>(PyBytes_FromStringAndSize(buf, dlen)));

        /* dispatch(kind: str, stream_id: int, data: bytes) — the old "sKy#" call. bp::call takes
         * the raw PyObject* callable directly; the result is a new-ref bp::object (RAII decref). */
        bp::object r = bp::call<bp::object>(dispatch_,
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

uint8_t *PyBridge::copy_capped(const uint8_t *src, size_t len, size_t *out_len) const
{
    size_t n = len > maxcopy_ ? maxcopy_ : len;
    if (n == 0) { *out_len = 0; return nullptr; }
    uint8_t *p = (uint8_t *)malloc(n);
    if (!p) { *out_len = 0; return nullptr; }   /* alloc failed → keep data/len consistent */
    if (src) memcpy(p, src, n);
    *out_len = n;
    return p;
}

void PyBridge::worker_main()
{
    const char *modname = getenv("AGY_PROC_MODULE");
    if (!modname || !*modname) modname = "pyagy.agy_process";
    const char *pypath = getenv("AGY_PROC_PYTHONPATH");
    const char *mc = getenv("AGY_PROC_MAXCOPY");
    if (mc && *mc) { long v = strtol(mc, NULL, 0); if (v > 0) maxcopy_ = (size_t)v; }

    Py_InitializeEx(0);  /* raw C-API bootstrap — Boost.Python has no embedding entry point */

    /* GIL is held from Py_InitializeEx until PyEval_SaveThread() below, so these bp::objects are
     * fine; all of them (sys/mod/disp) destruct before the GIL drops. */
    try {
        bp::object sys = bp::import(bp::str("sys"));
        sys.attr("is_agy_shim") = true;                 /* → Py_True */
        if (pypath && *pypath)
            sys.attr("path").attr("insert")(0, bp::str(pypath));   /* sys.path.insert(0, pypath) */

        bp::object mod = bp::import(bp::str(modname));
        bp::object disp = mod.attr("dispatch");         /* transient new-ref, destructs under the GIL */
        /* Pin ONE strong raw ref forever. dispatch_ MUST stay a raw PyObject* (not a bp::object) —
         * a bp::object member destroyed at exit would Py_DECREF GIL-less in a never-Py_Finalize'd
         * interp (UB); this mirrors Boost's own scope.hpp forever-ref idiom. */
        dispatch_ = bp::incref(disp.ptr());
    }
    catch (const bp::error_already_set &) {
        PYLOG("failed to init module '%s':", modname);
        if (PyErr_Occurred()) PyErr_Print();
        PyErr_Clear();
        dispatch_ = nullptr;                            /* → ok=false, graceful degrade */
    }

    int ok = (dispatch_ != NULL);
    PyThreadState *saved = PyEval_SaveThread();  /* release GIL while idle */

    {
        boost::unique_lock<boost::mutex> lk(qmu_);
        ready_ = ok ? 1 : 0;
        qcv_.notify_all();       /* wake start() (predicate: ready_ != -1) */
    }
    if (!ok) { PyEval_RestoreThread(saved); return; }
    PYLOG("worker ready (module=%s, maxcopy=%zu)", modname, maxcopy_);

    for (;;) {
        job *j;
        {
            boost::unique_lock<boost::mutex> lk(qmu_);
            qcv_.wait(lk, [this] { return head_ != nullptr || stop_; });
            if (!head_) break;    /* woken by stop_ with the queue drained → exit */
            j = head_;
            head_ = j->next;
            if (!head_) tail_ = nullptr;
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

int PyBridge::start()
{
    try {
        boost::thread::attributes attrs;
        attrs.set_stack_size(16u * 1024 * 1024);  /* big stack for libpython */
        worker_ = boost::thread(attrs, [this] { worker_main(); });
        /* Kept joinable (not detached) so shutdown() can join it at os.Exit. The singleton is
         * leaked, so worker_ is never destructed → no std::terminate even if shutdown never runs. */
    } catch (const std::exception &e) {
        PYLOG("boost::thread create failed: %s", e.what());
        ready_ = 0;
        return -1;
    }

    boost::unique_lock<boost::mutex> lk(qmu_);
    qcv_.wait(lk, [this] { return ready_ != -1; });
    return ready_ == 1 ? 0 : -1;
}

void PyBridge::emit(agy_event_t *ev)
{
    if (ready_ != 1) return;

    if (ev->mode == AGY_ASYNC) {
        job *j = new (std::nothrow) job();
        if (!j) return;
        j->on_heap = true;
        j->kind = ev->kind;
        j->stream_id = ev->stream_id;
        j->mode = AGY_ASYNC;
        j->data = copy_capped(ev->data, ev->len, &j->len);
        if (!enqueue(j)) { free(j->data); delete j; }   /* shutting down: drop it */
        return;
    }

    /* SYNC: stack job, block until the worker fills the verdict. boost::mutex/condition_variable
     * members are constructed/destroyed with the job — no manual init/destroy. */
    job j;
    j.kind = ev->kind;
    j.stream_id = ev->stream_id;
    j.mode = AGY_SYNC;
    j.data = copy_capped(ev->data, ev->len, &j.len);

    if (!enqueue(&j)) { free(j.data); return; }   /* shutting down: don't block on a dead worker */
    {
        boost::unique_lock<boost::mutex> lk(j.done_mu);
        j.done_cv.wait(lk, [&] { return j.done; });
    }

    ev->verdict = j.verdict;
    ev->out_data = j.out_data;   /* caller frees via agy_py_free */
    ev->out_len = j.out_len;
}

/* Cooperative teardown, invoked from the os.Exit hook (the one callback that fires under agy's Go
 * exit). Idempotent. Order at os.Exit: the "exit" marker is SYNC-emitted first (worker alive),
 * THEN this runs — so the marker is recorded before the worker stops. New emits after stop_ are
 * refused by enqueue(), so no goroutine blocks on a dead worker. */
void PyBridge::shutdown()
{
    {
        boost::unique_lock<boost::mutex> lk(qmu_);
        if (stop_) return;   /* already shut down */
        stop_ = true;
        ready_ = 0;          /* ready() → false; emit() fast-path no-ops */
    }
    qcv_.notify_all();       /* wake the worker so it drains the queue and exits */
    if (worker_.joinable()) worker_.join();
}

}  // anonymous namespace

/* C ABI entry points (pybridge.h) — thin delegators to the singleton. */
extern "C" int  agy_py_ready(void) { return bridge().ready() ? 1 : 0; }
extern "C" int  agy_py_start(void) { return bridge().start(); }
extern "C" void agy_py_emit(agy_event_t *ev) { bridge().emit(ev); }
extern "C" void agy_py_shutdown(void) { bridge().shutdown(); }

extern "C" void agy_py_free(agy_event_t *ev)   /* stateless: free a SYNC replacement buffer */
{
    if (ev->out_data) { free(ev->out_data); ev->out_data = nullptr; ev->out_len = 0; ev->verdict = 0; }
}
