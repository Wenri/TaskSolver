"""In-agy child runner for AgyProcess — make the shim's embedded interpreter act as a
REAL `multiprocessing.spawn` child, WITHOUT the process-ownership teardown that stock
`spawn_main`/`_bootstrap` would inflict on agy (which owns the process, not us).

Started on a daemon thread by `agy_process.__init__` when `AGY_MP_MODE=1`, so a blocking
`recv()` here can't starve the hook-dispatch worker (blocking releases the GIL).

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

_result_conn = None   # child end of the AgyProcess result Pipe; the target sends objects here


def get_result_conn():
    """The `multiprocessing.connection.Connection` back to the parent `AgyProcess`.
    A target running under AgyProcess calls this to stream native Python objects home."""
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
    from multiprocessing import connection, reduction
    boot = int(os.environ["AGY_MP_BOOT_FD"])
    chan = int(os.environ["AGY_MP_CHAN_FD"])
    _set_cloexec(boot)
    _set_cloexec(chan)                              # don't leak into agy's Go child processes
    _result_conn = connection.Connection(chan)      # duplex socketpair end inherited from the parent
    with os.fdopen(boot, "rb", closefd=True) as fp:
        prep = reduction.pickle.load(fp)
        _trimmed_prepare(prep)
        proc = reduction.pickle.load(fp)            # the AgyProcess instance (target + args)

    sys.stdin = None                                 # neutralization 1
    _orig_shutdown = threading._shutdown             # neutralization 2
    threading._shutdown = lambda: None
    try:
        exitcode = proc._bootstrap(parent_sentinel=None)   # REAL mp child path (runs target, returns exitcode)
    except BaseException as e:                       # firewall: an escaped error must never reach Py_Exit
        exitcode = 1
        try:
            _result_conn.send(("_agy_exc", "".join(traceback.format_exception(e))))
        except Exception:
            pass
    finally:
        threading._shutdown = _orig_shutdown
    try:
        _result_conn.send(("_agy_done", exitcode))   # completion sentinel (agy owns process lifetime)
    except Exception:
        pass


def main():
    try:
        _run()
    except Exception:
        traceback.print_exc()


def start():
    """Run the child on a daemon thread (called from agy_process import under AGY_MP_MODE)."""
    threading.Thread(target=main, name="agy-mp-child", daemon=True).start()


def stream_turns(kinds=None, max_wait=300):
    """Built-in AgyProcess target: stream agy's DECODED model turns home over the Connection
    as they're produced, until agy exits (the parent then sees EOF on recv()) or max_wait.
    Needs an instrumented stage that decodes turns (stage>=3 → genai_turn). Uses a subscribe
    → in-process queue → single-sender loop, so the send side has no Connection race."""
    import queue as _q
    from . import subscribe as _subscribe
    conn = get_result_conn()
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
            conn.send(obj)
        except (BrokenPipeError, OSError):
            break


# --- targets used by test_scripts/test_agyprocess.py (importable by reference in the child) ---
def _demo_target(*args, **kwargs):
    get_result_conn().send({"agy_mp": "ok", "args": args, "kwargs": kwargs,
                            "pid": os.getpid(), "py": sys.version.split()[0]})


def _raise_target(*args, **kwargs):
    raise ValueError("agy-mp boom")   # -> _bootstrap catches it, exitcode 1 over ("_agy_done", 1)
