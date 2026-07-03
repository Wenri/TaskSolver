"""AgyProcess — run agy as a `multiprocessing.spawn`-shaped child, streaming native
Python objects back over a Connection instead of a capture JSONL file.

    from pyagy.agyprocess import AgyProcess
    from pyagy.agy_process.mp_child import get_result_conn

    def work(prompt):                       # runs INSIDE agy (embedded interp)
        get_result_conn().send({"answer": 42})

    p = AgyProcess(target=work, args=("hi",))
    p.start()
    print(p.recv())                         # -> {"answer": 42}  (native object from the child)
    p.terminate(); p.join()

Design (plan why-make-agy-a-splendid-rainbow.md): the parent is a real `SpawnProcess`
with a custom Popen that execs agy under a PTY and hands the child two inheritable fds
(a result Pipe + a boot pipe with the pickled target). The child (shim's embedded interp,
`agy_process/mp_child.py`) runs the REAL `_bootstrap` with three neutralizations. The
process lifecycle (start/join/exitcode/terminate) is inherited from `popen_fork.Popen`
and tracks agy's own pid; the TASK result flows over the Connection (agy owns lifetime).
"""
import os
import threading
import traceback

import multiprocessing.connection as _conn
from multiprocessing import reduction, spawn as mp_spawn
from multiprocessing.context import SpawnProcess, set_spawning_popen
from multiprocessing.popen_fork import Popen as _ForkPopen

from ._env import ROOT, instrumented_env
from ._pty import PtyProcess
from .session import ensure_git_workspace

_VENDOR_AGY = os.path.join(ROOT, "vendor", "agy")   # the pinned agy whose build-id matches the shim


class AgyPopen(_ForkPopen):
    """Launch agy (not python) as the spawn child. Only `_launch` differs from the stock
    fork Popen; poll/wait/terminate/kill are inherited and act on agy's pid + PTY sentinel."""
    method = "agy"

    def _launch(self, process_obj):
        parent_conn, child_conn = _conn.Pipe(duplex=True)   # bare socketpair Pipe (no semaphore → WSL1-ok)
        boot_r, boot_w = os.pipe()
        os.set_inheritable(child_conn.fileno(), True)        # CLOEXEC off so they survive agy's execve
        os.set_inheritable(boot_r, True)

        agy = getattr(process_obj, "_agy_bin", None) or _VENDOR_AGY
        workdir = ensure_git_workspace(getattr(process_obj, "_workdir", None))
        capture = getattr(process_obj, "_capture", None) or os.path.join(workdir, "agy-capture.jsonl")
        env = instrumented_env(stage=getattr(process_obj, "_stage", 1), capture=capture,
                               extra_env={"AGY_MP_MODE": "1",
                                          "AGY_MP_CHAN_FD": str(child_conn.fileno()),
                                          "AGY_MP_BOOT_FD": str(boot_r)})
        argv = [agy, *(getattr(process_obj, "_agy_args", None) or ["--print", "agy-mp"])]

        self._parent_conn = parent_conn
        self._stop = threading.Event()
        self._pty = PtyProcess()
        self._pty.spawn(argv, workdir, env)                  # pty.fork + execve(agy); child inherits the fds
        self.pid = self._pty.pid
        self.sentinel = self._pty.fd                         # PTY master EOFs on agy death (wait(timeout))
        self.finalizer = None

        # Pickle (prep_data, process_obj) UNDER set_spawning_popen — BaseProcess.__reduce__ and
        # the AuthenticationString refuse to pickle outside the spawning context (stock _launch
        # wraps it the same way). Dump synchronously here (no thread-race on the module-global),
        # then stream the bytes into the boot pipe from a thread so a large payload can't
        # deadlock the launch before the child reads.
        import io
        prep = mp_spawn.get_preparation_data(process_obj._name)
        buf = io.BytesIO()
        set_spawning_popen(self)
        try:
            reduction.dump(prep, buf)
            reduction.dump(process_obj, buf)
        finally:
            set_spawning_popen(None)
        payload = buf.getvalue()

        def _feed():
            try:
                with os.fdopen(boot_w, "wb", closefd=True) as f:
                    f.write(payload)
            except Exception:
                traceback.print_exc()
        threading.Thread(target=_feed, name="agy-mp-boot", daemon=True).start()

        child_conn.close()                                   # parent keeps only parent_conn
        try:
            os.close(boot_r)                                 # child has its own inherited copy
        except OSError:
            pass

        threading.Thread(target=self._pump, name="agy-mp-pty", daemon=True).start()

    def _pump(self):
        """Drain agy's PTY + auto-answer terminal-capability queries so its TUI proceeds."""
        try:
            while not self._stop.is_set():
                self._pty.pump(0.3)
        except Exception:
            pass

    def close(self):
        self._stop.set()
        try:
            if getattr(self, "_parent_conn", None):
                self._parent_conn.close()
        except Exception:
            pass
        try:
            if self._pty is not None and self._pty.fd is not None:
                os.close(self._pty.fd)
                self._pty.fd = None
        except Exception:
            pass


class AgyProcess(SpawnProcess):
    """`multiprocessing.Process`-shaped handle for an agy run. `target` executes inside
    agy's embedded interpreter; use `pyagy.agy_process.mp_child.get_result_conn()` there to
    send objects home, and `AgyProcess.recv()` / `.poll()` here to read them."""

    @staticmethod
    def _Popen(process_obj):
        return AgyPopen(process_obj)

    def __init__(self, target=None, name=None, args=(), kwargs=None, *,
                 agy_bin=None, agy_args=None, workdir=None, stage=1, capture=None,
                 persistent=False, daemon=None):
        super().__init__(group=None, target=target, name=name,
                         args=args, kwargs=(kwargs or {}), daemon=daemon)
        self._agy_bin = agy_bin        # agy binary (default: the pinned vendor/agy)
        self._agy_args = agy_args      # agy argv tail (default: ["--print", "agy-mp"])
        self._workdir = workdir        # git workspace (default: a throwaway repo)
        self._stage = stage            # AGY_PROC_STAGE for the shim capture pipeline
        self._capture = capture
        self._persistent = persistent  # (reserved) keep agy alive across multiple turns

    # --- parent-side result channel (native Python objects sent by the child target) ---
    def recv(self):
        return self._popen._parent_conn.recv()

    def poll(self, timeout=0.0):
        return self._popen._parent_conn.poll(timeout)

    @property
    def connection(self):
        return self._popen._parent_conn
