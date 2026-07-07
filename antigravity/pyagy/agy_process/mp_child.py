"""In-agy child runner for AgyProcess — make the shim's embedded interpreter act as a
REAL `multiprocessing.spawn` child, WITHOUT the process-ownership teardown that stock
`spawn_main`/`_bootstrap` would inflict on agy (which owns the process, not us).

Started on a daemon thread by `agy_process.__init__` when the worker channel is wired
(`WIRE_MP_BOOT_FD` is set), so a blocking `get()` here can't starve the hook-dispatch worker
(blocking releases the GIL).

The three neutralizations (see plan why-make-agy-a-splendid-rainbow.md):
  * `sys.stdin = None`            → `util._close_stdin()` early-returns (won't close the PTY's fd 0)
  * `threading._shutdown` no-op   → `_bootstrap`'s finally won't tear down agy's threads
  * trimmed `prepare()`           → apply authkey/name only; skip sys.path=/os.chdir/_fixup_main
We call `proc._bootstrap()` DIRECTLY (not `spawn_main`), which also skips its `sys.exit()`
and the `is_forking(sys.argv)` assert. `_bootstrap` runs the target and RETURNS the exitcode.
"""
import os
import sys
import threading
import time
import traceback

_result_conn = None   # the result SimpleQueue (child side), sourced from proc._args[0] in _run;
#                       the target puts objects here (also handed to the target as its arg0)


def get_result_conn():
    """The result `SimpleQueue` back to the parent (child side) — the same object handed to the
    target as arg0 (``args=(q,)``), also exposed here so a target can fetch it without threading it
    through. A target calls ``.put(obj)`` on it to stream native Python objects home."""
    return _result_conn


def _set_cloexec(fd):
    import fcntl
    fcntl.fcntl(fd, fcntl.F_SETFD, fcntl.fcntl(fd, fcntl.F_GETFD) | fcntl.FD_CLOEXEC)


def _trimmed_prepare(data):
    # Only what a child legitimately needs; SKIP the process-hostile bits of the stock
    # spawn.prepare(): sys.path= (drops the shim's PYTHONPATH), os.chdir (moves agy's cwd),
    # sys.argv=, and _fixup_main_* (re-imports the parent's __main__ into agy).
    from multiprocessing import process
    if data.get("name"):
        process.current_process().name = data["name"]
    if "authkey" in data:
        process.current_process().authkey = data["authkey"]
    if "start_method" in data:
        from multiprocessing import spawn as _spawn
        _spawn.set_start_method(data["start_method"], force=True)


def _run():
    global _result_conn
    from multiprocessing import reduction, resource_tracker
    boot = int(os.environ["WIRE_MP_BOOT_FD"])
    # keep boot open in-process (it's the parent-death sentinel), but CLOEXEC so agy's own Go
    # child processes don't inherit it
    _set_cloexec(boot)
    with os.fdopen(boot, "rb", closefd=False) as fp:  # closefd=False: boot outlives the read — the parent
        tracker_fd = reduction.pickle.load(fp)      #   holds its write end open, so boot's EOF = our death
        # Re-attach to the parent's resource_tracker (as spawn_main does) so the queue's SemLocks share
        # it instead of this interpreter booting a second tracker. CLOEXEC: keep it in-process; don't
        # leak it into agy's Go child processes. Installed BEFORE proc unpickles, so the queue's
        # SemLocks (rebuilt with proc's args) re-attach to this tracker rather than a fresh one.
        resource_tracker._resource_tracker._fd = tracker_fd
        _set_cloexec(tracker_fd)
        prep = reduction.pickle.load(fp)
        _trimmed_prepare(prep)
        proc = reduction.pickle.load(fp)            # the AgyProcess instance (target + args)

    # The result SimpleQueue rides proc's args (arg0 = result conn, stock-mp style: the caller passes
    # it via AgyProcess(target=..., args=(q,))). Pull it out for get_result_conn() + the firewall
    # puts below; the pipe fds were inherited across execve, the SemLocks sem_opened by name.
    _result_conn = proc._args[0]
    _result_conn._reader.close()                    # child only puts; drop the inherited reader end

    sys.stdin = None                                 # neutralization 1
    _orig_shutdown = threading._shutdown             # neutralization 2
    threading._shutdown = lambda: None
    try:
        # boot doubles as the parent-death sentinel (so parent_process().is_alive()/.join() work): the
        # parent writes the payload once, then holds boot's write end open for its whole life, so boot
        # goes readable only when that end closes (EOF = our death). A dedicated write-once pipe like
        # stock multiprocessing's child_r/parent_w — no data ever rides it, so nothing spuriously trips it.
        exitcode = proc._bootstrap(parent_sentinel=boot)   # REAL mp child path (runs target, returns exitcode)
    except BaseException as e:                       # firewall: an escaped error must never reach Py_Exit
        exitcode = 1
        try:
            _result_conn.put(("_agy_exc", "".join(traceback.format_exception(e))))
        except Exception:
            pass
    finally:
        threading._shutdown = _orig_shutdown
    try:
        _result_conn.put(("_agy_done", exitcode))   # completion sentinel (agy owns process lifetime)
    except Exception:
        pass


def main():
    try:
        _run()
    except Exception:
        traceback.print_exc()


def start():
    """Run the child on a daemon thread (called from agy_process import when the embedded-worker
    channel is wired). No-op if the boot fd is absent or stale."""
    try:
        boot = int(os.environ.get("WIRE_MP_BOOT_FD", "-1"))
        if boot < 0:
            return
        os.fstat(boot)
    except (ValueError, OSError):
        return
    threading.Thread(target=main, name="agy-mp-child", daemon=True).start()


def stream_turns(conn, kinds=None, max_wait=300):
    """Built-in AgyProcess target: stream agy's DECODED model turns home over the result queue
    `conn` — arg0, passed by the caller via ``AgyProcess(target=stream_turns, args=(q,))`` (stock-mp
    style) — as they're produced, until agy exits (the parent then sees EOF on get()) or max_wait.
    The shim always installs the capture hooks, so genai_turn events flow. Uses a subscribe
    → in-process queue → single-sender loop, so the put side has no cross-thread race."""
    import queue as _q
    from . import subscribe as _subscribe
    kinds = set(kinds or ("genai_turn", "app_response", "resp_text", "resp_thinking"))
    q = _q.Queue()
    _subscribe(lambda obj: q.put(obj) if isinstance(obj, dict) and obj.get("kind") in kinds else None)
    end = time.time() + max_wait
    while time.time() < end:
        try:
            obj = q.get(timeout=1.0)
        except _q.Empty:
            continue
        try:
            conn.put(obj)
        except (BrokenPipeError, OSError):
            break


# --- targets used by test_scripts/test_agyprocess.py (importable by reference in the child) ---
# All targets receive the result conn as arg0 (the caller passes args=(q, ...)), stock-mp style.
def _demo_target(conn, *args, **kwargs):
    # also probes the parent-death sentinel: parent_process() reflects the controlling process,
    # and is_alive() is True here because the parent is alive (its boot-pipe write end is open).
    import multiprocessing as _mp
    parent = _mp.parent_process()
    conn.put({"agy_mp": "ok", "args": args, "kwargs": kwargs,
              "pid": os.getpid(), "py": sys.version.split()[0],
              "parent_alive": parent.is_alive(), "ppid": parent.pid})


def _raise_target(conn, *args, **kwargs):
    raise ValueError("agy-mp boom")   # -> _bootstrap catches it, exitcode 1 over ("_agy_done", 1)
