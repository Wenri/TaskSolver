"""Run a multiprocessing target inside the linked CLI's embedded interpreter.

WirePopen supplies a boot pipe (``WIRE_MP_BOOT_FD``) and a caller-owned result
SimpleQueue. This module unpickles the spawn payload and runs the target on a daemon
thread, so blocking queue operations do not stop the book-dispatch worker.

The CLI retains process ownership. Calling ``proc._bootstrap`` directly avoids
``spawn_main``'s argv check and sys.exit. Three adjustments protect the host:
  * ``sys.stdin = None`` prevents multiprocessing from closing the host's fd 0.
  * A temporary ``threading._shutdown`` no-op prevents host-thread teardown.
  * Preparation applies the name, authkey and start method while preserving the
    host's sys.path, cwd, argv and __main__.

Imports remain stdlib-only. ``stream_turns`` loads the provider's ``WIRE_MODULE``
at call time to subscribe to its recorder.
"""
from __future__ import annotations

import os
import pickle
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from multiprocessing.connection import Connection
from typing import Protocol, cast

from ..types import Event


class ResultWriter(Protocol):
    def put(self, obj: object) -> None: ...


class _ChildChannel(ResultWriter, Protocol):
    _reader: Connection[object, object]
    _writer: Connection[object, object]


class _BootProcess(Protocol):
    _args: tuple[object, ...]
    def _bootstrap(self, parent_sentinel: int) -> int: ...


class _SubscriberModule(Protocol):
    def subscribe(self, fn: Callable[[Event], object]) -> None: ...


class _ThreadingState(Protocol):
    _shutdown: Callable[[], None]


class _TrackerState(Protocol):
    _fd: int | None


__all__ = ["DONE", "EXC", "get_result_conn", "main", "start", "stream_turns",
           "_demo_target", "_raise_target"]

# Tuple tags distinguish target completion and failures from streamed event dictionaries.
DONE = "_wire_done"   # ("_wire_done", exitcode) — normal completion (host owns process lifetime)
EXC = "_wire_exc"     # ("_wire_exc", traceback) — the target raised; firewalled off Py_Exit

_result_conn: _ChildChannel | None = None   # proc._args[0], also passed to the target


def get_result_conn() -> ResultWriter | None:
    """Return the target's result queue; ``put(obj)`` sends an object to the parent."""
    return _result_conn


def _set_cloexec(fd: int) -> None:
    import fcntl
    fcntl.fcntl(fd, fcntl.F_SETFD, fcntl.fcntl(fd, fcntl.F_GETFD) | fcntl.FD_CLOEXEC)


def _trimmed_prepare(data: Mapping[str, object]) -> None:
    # Preserve the host's import paths, cwd, argv and __main__ while preparing the target.
    from multiprocessing import process
    name = data.get("name")
    if isinstance(name, str) and name:
        process.current_process().name = name
    authkey = data.get("authkey")
    if isinstance(authkey, bytes):
        process.current_process().authkey = authkey
    if "start_method" in data:
        from multiprocessing import set_start_method
        method = data["start_method"]
        if method is None or isinstance(method, str):
            set_start_method(method, force=True)


def _run() -> None:
    global _result_conn
    from multiprocessing import resource_tracker
    boot = int(os.environ["WIRE_MP_BOOT_FD"])
    # Keep the parent-death sentinel open here, but prevent inheritance across exec.
    _set_cloexec(boot)
    with os.fdopen(boot, "rb", closefd=False) as fp:  # boot must outlive payload reading
        tracker_fd = cast(int, pickle.load(fp))
        # Attach the parent's tracker before unpickling the queue's SemLocks, so
        # they share it rather than starting another tracker. Keep it out of exec'd children.
        cast(_TrackerState, resource_tracker._resource_tracker)._fd = tracker_fd
        _set_cloexec(tracker_fd)
        prep = cast(Mapping[str, object], pickle.load(fp))
        _trimmed_prepare(prep)
        proc = cast(_BootProcess, pickle.load(fp))            # the WireProcess instance (target + args)

    # Queue pipe fds survive exec; its SemLocks are reopened by name during unpickling.
    _result_conn = cast(_ChildChannel, proc._args[0])
    _result_conn._reader.close()                    # child only puts; drop the inherited reader end
    # Tool subprocesses must not inherit the writer and prevent EOF when the host exits.
    _set_cloexec(_result_conn._writer.fileno())

    sys.stdin = None                                 # protect the host's fd 0
    thread_state = cast(_ThreadingState, threading)
    _orig_shutdown = thread_state._shutdown
    thread_state._shutdown = lambda: None
    try:
        # After the payload is consumed, boot's EOF signals parent death. The parent
        # holds its write end open for its lifetime and sends no further data.
        exitcode = proc._bootstrap(parent_sentinel=boot)
    except BaseException as e:                       # firewall: an escaped error must never reach Py_Exit
        exitcode = 1
        try:
            _result_conn.put((EXC, "".join(traceback.format_exception(e))))
        except Exception:
            pass
    finally:
        thread_state._shutdown = _orig_shutdown
    try:
        _result_conn.put((DONE, exitcode))          # completion sentinel (host owns process lifetime)
    except Exception:
        pass


def main() -> None:
    try:
        _run()
    except Exception:
        traceback.print_exc()


def start() -> None:
    """Start the target thread when an embedded interpreter has a live boot fd.

    Safe to call from every WIRE_MODULE import: ordinary capture runs have no
    worker channel, and startup failures must not interrupt the host's import.
    """
    try:
        if not cast(object, getattr(sys, "_wire_shim", False)):
            return
        boot = int(os.environ.get("WIRE_MP_BOOT_FD", "-1"))
        if boot < 0:
            return
        os.fstat(boot)
    except (ValueError, OSError):
        return
    except Exception:
        traceback.print_exc()
        return
    threading.Thread(target=main, name="wire-mp-child", daemon=True).start()


def stream_turns(conn: ResultWriter, kinds: Sequence[str] | None = None,
                 max_wait: float | None = None) -> None:
    """Stream recorder events through the result queue passed as the target's first argument.

    Keep ``max_wait=None`` for persistent sessions. A finite deadline returns from
    this target and sends DONE while the CLI may still be running, ending collection
    and losing subsequent turns. Any cap must exceed the parent's collection budget.

    Subscriptions come from the live WIRE_MODULE and are removed on exit to prevent
    abandoned queues from growing. ``kinds=None`` forwards every event, including raw
    records; specifying kinds avoids unnecessary pipe traffic. A local queue funnels
    recorder callbacks through one sender so writes cannot race across threads.
    """
    import importlib
    import queue as _q
    mod = cast(_SubscriberModule, importlib.import_module(os.environ["WIRE_MODULE"]))
    want = set(kinds) if kinds else None
    q: _q.Queue[Event] = _q.Queue()

    def _sub(obj: Event) -> None:
        kind = obj.get("kind")
        if isinstance(kind, str) and kind and (want is None or kind in want):
            q.put(obj)

    mod.subscribe(_sub)
    end = None if max_wait is None else time.time() + max_wait
    try:
        while end is None or time.time() < end:
            try:
                obj = q.get(timeout=1.0)
            except _q.Empty:
                continue
            try:
                conn.put(obj)
            except (BrokenPipeError, OSError):
                break
    finally:
        unsub = cast(Callable[[Callable[[Event], object]], None] | None, getattr(mod, "unsubscribe", None))   # tolerate a provider module without it
        if unsub is not None:
            unsub(_sub)


# --- targets used by test_scripts/test_agyprocess.py (importable by reference in the child) ---
# All targets receive the result conn as arg0 (the caller passes args=(q, ...)), stock-mp style.
def _demo_target(conn: ResultWriter, *args: object, **kwargs: object) -> None:
    # also probes the parent-death sentinel: parent_process() reflects the controlling process,
    # and is_alive() is True here because the parent is alive (its boot-pipe write end is open).
    import multiprocessing as _mp
    parent = _mp.parent_process()
    assert parent is not None
    conn.put({"wire_mp": "ok", "args": args, "kwargs": kwargs,
              "pid": os.getpid(), "py": sys.version.split()[0],
              "parent_alive": parent.is_alive(), "ppid": parent.pid})


def _raise_target(conn: ResultWriter, *args: object, **kwargs: object) -> None:
    raise ValueError("wire-mp boom")   # -> _bootstrap catches it, exitcode 1 over (EXC, tb) then (DONE, 1)
