"""Environment + argv wiring for an instrumented agy run: run the linked ``vendor/agy`` host
executable (which links ``vendor/libagy.so`` as its first ``DT_NEEDED`` dependency) and wire the
in-process Python subsystem (``pyagy.agy_process``) + capture JSONL. Used by
:class:`pyagy.agyprocess.AgyProcess` (the single agy launcher) and test_scripts/run-agy.sh.

Because ``vendor/agy`` is a normal ELF executable linked against ``libagy.so`` with a ``RUNPATH``,
no ``LD_PRELOAD`` or ``ld.so --preload`` wrapper is needed, and child processes spawned by ``agy``
do not inherit any preload environment variables.
"""
import os

from wirecap.runtime.vendor import vendored

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))   # .../pyagy


def _vendored(in_pkg_rel, sibling_rel):
    """agy-side binding of the shared resolver (wirecap.runtime.vendor)."""
    return vendored(_PKG_DIR, "pyagy", in_pkg_rel, sibling_rel)


# The bundled pyagy/vendor/libagy.so (wheel) or the sibling antigravity/vendor/libagy.so
# (checkout) — build-id coupled to the vendor/agy host executable (same build).
LIBAGY = _vendored("vendor/libagy.so", "../vendor/libagy.so")
SHIM = LIBAGY


def preload_argv(agy_bin, agy_args, shim=None, env=None):
    """Build the argv that runs ``agy_bin`` (linked directly against ``libagy.so``) and record
    ``AGY_PROC_REAL_EXE`` in ``env`` for the ``readlink_filter`` book."""
    del shim
    env = os.environ if env is None else env
    env["AGY_PROC_REAL_EXE"] = os.path.abspath(agy_bin)
    return [agy_bin, *agy_args]


def instrumented_env(capture="agy-capture.jsonl", log=None,
                     module="pyagy.agy_process",
                     base=None, extra_env=None):
    """Environment for an instrumented agy run: points the shim's embedded interpreter at
    ``module`` (default ``pyagy.agy_process``) and writes book events to the ``capture`` JSONL.
    The shim installs the full working book union (wire + app + rpc) on every run — the only gate
    is ``AGY_PROC_ENABLE`` (set here). This sets NO ``LD_PRELOAD``/``LD_LIBRARY_PATH``/``PYTHONPATH``:
    the shim is injected via :func:`preload_argv` (the loader's ``--preload``/``--library-path``) and
    the embedded interpreter finds pyagy/wirecap via ``site`` (see ``PYTHONHOME`` below), so nothing
    shim-related leaks into agy's children. Mirrors run-agy.sh; ``extra_env`` (applied last) can
    override any AGY_PROC* knob, and a ``None`` value removes that variable (e.g. the SSH_*
    variables, which make agy skip its keyring login)."""
    env = dict(base if base is not None else os.environ)
    env.update({
        "AGY_PROC_ENABLE": "1",
        # WIRE_MODULE is the shared native bridge's contract (wirecap/native). The bridge no longer
        # takes a sys.path from us: Py_InitializeEx runs `site`, so the embedded interpreter imports
        # pyagy.agy_process (+ wirecap) from its own env's site-packages — the same install the parent
        # runs, pinned to that env by PYTHONHOME below. No PYTHONPATH is exported into agy's children.
        "WIRE_MODULE": module,
        # WIRE_CAPTURE is the shared decode-layer name (pycodex uses it too); agy_process
        # still accepts the legacy AGY_PROC_CAPTURE as a fallback.
        "WIRE_CAPTURE": os.path.abspath(capture),
        # install the os.OpenFile conversation-id probe (overlay) so instrumented runs learn
        # the exact conversation id in-process (agy doesn't expose it via env); newest-*.db-by-
        # mtime is the fallback. The file_open trampoline is installed only when this is set,
        # and its C-side path filter keeps it cheap (Python sees only conversation-store opens).
        "AGY_PROC_CONV_ID": "1",
        "GODEBUG": ("netdns=cgo," + env.get("GODEBUG", "")).rstrip(","),
        "TERM": env.get("TERM", "xterm-256color"),
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",   # disable agy's background self-update
    })
    # Point the embedded interpreter's prefix at the launching env (mirrors pycodex) so `site`
    # resolves pyagy/wirecap from THIS env's site-packages — the same copy the parent imported —
    # rather than the shim's build-env prefix. The env must be the shim's Python version.
    if env.get("CONDA_PREFIX"):
        env.setdefault("PYTHONHOME", env["CONDA_PREFIX"])
    if log:
        env["AGY_PROC_LOG"] = os.path.abspath(log)
    for key, value in (extra_env or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env
