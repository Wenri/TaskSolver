/* Shared CPython capture worker on a 16 MB Boost.Thread stack.
 * Boost.Python objects stay inside GIL-held dispatch calls. The dispatch callable
 * is pinned for the process lifetime because the host never finalizes CPython.
 * Queued jobs own their payload; synchronous callers wait on a stack-owned promise.
 * Shutdown refuses new jobs, drains accepted jobs, then joins the worker.
 */
#define PY_SSIZE_T_CLEAN   /* Py_ssize_t lengths in Python C-API formats */
#include <boost/python.hpp>   /* pulls in Python.h first, with the right guards */
#include "wirecap.h"

#include <boost/thread.hpp>
#include <boost/thread/condition_variable.hpp>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <future>
#include <queue>
#include <utility>
#include <vector>

namespace bp = boost::python;

#define PYLOG(...) do { std::fprintf(stderr, "[wirecap/py] " __VA_ARGS__); std::fputc('\n', stderr); } while (0)

namespace {

/* The worker's output for a SYNC event: verdict=1 means `out` holds a replacement buffer. */
struct Result {
    int verdict = 0;
    std::vector<uint8_t> out;
};

/* A queued event. Held by value in the queue (movable: vector + trivial members), so no new/delete
 * and no ownership flag. `result` is non-owning: for SYNC it points at the emitter's stack promise
 * (fulfilled by the worker); for ASYNC it stays null (no promise → no cost on the hot path). */
struct job {
    const char          *kind = nullptr;    /* static literal, not owned */
    uint64_t             stream_id = 0;
    std::vector<uint8_t> data;              /* input bytes (owned; auto-freed) */
    wire_mode_t           mode = WIRE_ASYNC;
    std::promise<Result> *result = nullptr; /* SYNC: emitter's stack promise; ASYNC: null */
};

/* PyBridge — owns the embedded interpreter's worker thread, the job queue, and the pinned
 * dispatch callable. A single instance (see bridge()) lives for the whole process; the extern "C"
 * entry points delegate to it. */
class PyBridge {
public:
    int  start();               /* create the worker thread; block until it signals ready */
    void emit(wire_event_t *ev); /* enqueue an event (ASYNC) or block for a verdict (SYNC) */
    bool ready() const { return ready_ == 1; }
    void shutdown();            /* cooperatively stop + join the worker; idempotent */
    ~PyBridge() { shutdown(); } /* fallback: join the worker BEFORE members destruct, so a dtor on
                                 * a libc-exit path can't terminate on a joinable thread or tear
                                 * down an in-use mutex. Normally a no-op: agy's os.Exit never runs
                                 * it at all, and codex has already called shutdown() from main().
                                 * shutdown() is idempotent, so the double call is free. */

private:
    void   worker_main();       /* boost::thread body: init the interpreter, then drain the queue */
    bool   enqueue(job &&j);    /* false if shutting down (job dropped) */
    Result run_dispatch(const job &j);   /* on the worker thread, holding the GIL */
    std::vector<uint8_t> copy_capped(const uint8_t *src, size_t len) const;

    /* One mutex + condvar for everything: the start handshake, the job queue, and shutdown. qcv_
     * carries three signals (ready_ set / job enqueued / stop_ set), each disambiguated by the
     * waiter's predicate — start() waits on `ready_ != -1`, the worker loop on `!queue_.empty() || stop_`. */
    boost::thread             worker_;
    boost::mutex              qmu_;
    boost::condition_variable qcv_;
    std::queue<job>           queue_;                  /* FIFO of pending jobs (by value; guarded by qmu_) */
    bool                      stop_ = false;           /* set by shutdown(); worker drains then exits */
    bool                      stopped_ = false;        /* join completed; guarded by qmu_ */
    std::atomic<int>          ready_{-1};              /* readiness also read outside qmu_ by emitters */
    size_t                    maxcopy_ = 1u << 20;
    PyObject                 *dispatch_ = nullptr;     /* pyagy.agy_process.dispatch; strong raw ref */
};

/* Frontends stop the bridge before exiting: agy's os.Exit book, Codex main,
 * and Kimi's addon cleanup. The destructor handles ordinary C++ exit paths. */
PyBridge &bridge() { static PyBridge b; return b; }

bool PyBridge::enqueue(job &&j)
{
    boost::unique_lock<boost::mutex> lk(qmu_);
    if (stop_) return false;      /* shutting down: refuse so no emitter blocks on a dead worker */
    queue_.push(std::move(j));
    qcv_.notify_one();
    return true;
}

/* Called on the worker thread, holding the GIL. Every bp::object below is created AND destroyed
 * inside this GIL window — none escapes to a GIL-less context. Returns the SYNC verdict/bytes. */
Result PyBridge::run_dispatch(const job &j)
{
    Result res;
    if (!dispatch_) return res;
    try {
        /* The "y#" payload must be real Python bytes — bp::str/std::string would build PyUnicode
         * (str) and corrupt binary data. Build bytes via the C-API and adopt into a handle (throws
         * error_already_set on NULL). Empty buffer → 0-length bytes. */
        const char *buf = j.data.empty() ? "" : (const char *)j.data.data();
        Py_ssize_t  dlen = (Py_ssize_t)j.data.size();
        bp::object arg(bp::handle<>(PyBytes_FromStringAndSize(buf, dlen)));

        /* dispatch(kind: str, stream_id: int, data: bytes) — the old "sKy#" call. bp::call takes
         * the raw PyObject* callable directly; the result is a new-ref bp::object (RAII decref). */
        bp::object r = bp::call<bp::object>(dispatch_,
                                            bp::str(j.kind),
                                            (unsigned long long)j.stream_id,
                                            arg);

        if (j.mode == WIRE_SYNC && !r.is_none() && PyBytes_Check(r.ptr())) {
            const char *p = PyBytes_AS_STRING(r.ptr());
            res.out.assign(p, p + PyBytes_GET_SIZE(r.ptr()));
            res.verdict = 1;
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
    return res;
}

std::vector<uint8_t> PyBridge::copy_capped(const uint8_t *src, size_t len) const
{
    std::vector<uint8_t> v;
    size_t n = len > maxcopy_ ? maxcopy_ : len;
    if (n) {
        v.resize(n);
        if (src) std::memcpy(v.data(), src, n);
    }
    return v;
}

void PyBridge::worker_main()
{
    const char *modname = std::getenv("WIRE_MODULE");
    if (!modname || !*modname) modname = "pyagy.agy_process";
    const char *mc = std::getenv("WIRE_MAXCOPY");
    if (mc && *mc) { long v = std::strtol(mc, nullptr, 0); if (v > 0) maxcopy_ = (size_t)v; }

    Py_InitializeEx(0);  /* raw C-API bootstrap — Boost.Python has no embedding entry point */

    /* GIL is held from Py_InitializeEx until PyEval_SaveThread() below, so these bp::objects are
     * fine; all of them (sys/mod/disp) destruct before the GIL drops. */
    try {
        bp::object sys = bp::import(bp::str("sys"));
        sys.attr("_wire_shim") = true;                 /* → Py_True */
        /* No sys.path munging: Py_InitializeEx(0) runs `site`, so the embedded interpreter's own
         * prefix (PYTHONHOME points it at the launching env) already carries site-packages. WIRE_MODULE
         * (wirecap.* + pyagy/pycodex) is imported from that env's normal install — same copy the parent
         * runs. The env must be the same Python version the artifact was built against. */
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

    int ok = (dispatch_ != nullptr);
    PyThreadState *saved = PyEval_SaveThread();  /* release GIL while idle */

    {
        boost::unique_lock<boost::mutex> lk(qmu_);
        ready_ = ok && !stop_ ? 1 : 0;
        qcv_.notify_all();       /* wake start() (predicate: ready_ != -1) */
    }
    if (!ok) { PyEval_RestoreThread(saved); return; }
    PYLOG("worker ready (module=%s, maxcopy=%zu)", modname, maxcopy_);

    for (;;) {
        job j;
        {
            boost::unique_lock<boost::mutex> lk(qmu_);
            qcv_.wait(lk, [this] { return !queue_.empty() || stop_; });
            if (queue_.empty()) break;    /* woken by stop_ with the queue drained → exit */
            j = std::move(queue_.front());
            queue_.pop();
        }

        PyGILState_STATE gil = PyGILState_Ensure();
        Result res = run_dispatch(j);
        PyGILState_Release(gil);

        if (j.result)                          /* SYNC: fulfill the emitter's future (last use of j) */
            j.result->set_value(std::move(res));
        /* j (and its data vector) destructs at the end of the iteration — RAII, no free/delete. */
    }
    (void)saved;
}

int PyBridge::start()
{
    boost::unique_lock<boost::mutex> lk(qmu_);
    if (stop_) return -1;
    if (!worker_.joinable() && ready_ == -1) {
        try {
            boost::thread::attributes attrs;
            attrs.set_stack_size(16u * 1024 * 1024);
            worker_ = boost::thread(attrs, [this] { worker_main(); });
        } catch (const std::exception &e) {
            PYLOG("boost::thread create failed: %s", e.what());
            ready_ = 0;
            qcv_.notify_all();
            return -1;
        }
    }
    qcv_.wait(lk, [this] { return ready_ != -1; });
    return ready_ == 1 ? 0 : -1;
}

void PyBridge::emit(wire_event_t *ev)
{
    ev->verdict = 0;
    ev->out_len = 0;
    if (ready_ != 1) return;

    /* One job for both modes; they differ only in whether a result promise is attached. */
    job j{ .kind = ev->kind, .stream_id = ev->stream_id,
           .data = copy_capped(ev->data, ev->len), .mode = ev->mode };

    if (ev->mode == WIRE_ASYNC) {          /* fire-and-forget (on refuse the job just destructs here) */
        enqueue(std::move(j));
        return;
    }

    /* SYNC: block on the worker's verdict via a promise/future kept on THIS stack; the moved-in job
     * carries a pointer to the promise, so the worker's set_value reaches us. */
    std::promise<Result> pr;
    std::future<Result>  fut = pr.get_future();
    j.result = &pr;
    if (!enqueue(std::move(j))) return;   /* shutting down: fut is never got → no broken_promise */

    Result res = fut.get();
    /* Apply an equal-or-shorter replacement IN PLACE, on this (the caller's) thread. The sole
     * consumer rewrites ev->data (the egress buffer) with these bytes anyway, and we're running
     * synchronously on the caller's thread with its buffer still live — so write them straight into
     * ev->data and just report the new length. No out_data buffer, no malloc, no thread-local. */
    if (res.verdict && res.out.size() <= ev->len) {
        if (!res.out.empty()) std::memcpy((void *)ev->data, res.out.data(), res.out.size());
        ev->out_len = res.out.size();
        ev->verdict = 1;
    }
}

/* Cooperative teardown, invoked from the os.Exit book (the one callback that fires under agy's Go
 * exit). Idempotent. Order at os.Exit: the "exit" marker is SYNC-emitted first (worker alive),
 * THEN this runs — so the marker is recorded before the worker stops. New emits after stop_ are
 * refused by enqueue(), so no goroutine blocks on a dead worker. */
void PyBridge::shutdown()
{
    {
        boost::unique_lock<boost::mutex> lk(qmu_);
        if (stop_) {
            qcv_.wait(lk, [this] { return stopped_; });
            return;
        }
        stop_ = true;
        ready_ = 0;          /* ready() → false; emit() fast-path no-ops */
    }
    qcv_.notify_all();       /* wake the worker so it drains the queue and exits */
    if (worker_.joinable()) worker_.join();
    {
        boost::unique_lock<boost::mutex> lk(qmu_);
        stopped_ = true;
    }
    qcv_.notify_all();
}

}  // anonymous namespace

/* C ABI entry points (wirecap.h) — thin delegators to the singleton. */
extern "C" int  wire_ready(void) { return bridge().ready() ? 1 : 0; }
extern "C" int  wire_start(void) { return bridge().start(); }
extern "C" void wire_emit(wire_event_t *ev) { bridge().emit(ev); }
extern "C" void wire_shutdown(void) { bridge().shutdown(); }

/* Scalar-arg ASYNC convenience (easy FFI): build a transient event and emit it. */
extern "C" void wire_emit_async(const char *kind, uint64_t stream_id,
                                const uint8_t *data, size_t len)
{
    wire_event_t ev = { kind, stream_id, data, len, WIRE_ASYNC, 0, 0 };
    bridge().emit(&ev);
}
