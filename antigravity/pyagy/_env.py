"""Launch the linked agy host and configure its embedded Python capture worker.

The host loads libagy through DT_NEEDED/RUNPATH; no preload wrapper is needed.
Used by AgyProcess and test_scripts/run-agy.sh.
"""
import os
from collections.abc import Mapping, MutableMapping, Sequence

from wirecap.runtime.vendor import python_home, vendored

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))   # .../pyagy


def _vendored(in_pkg_rel: str, sibling_rel: str) -> str:
    """agy-side binding of the shared resolver (wirecap.runtime.vendor)."""
    return vendored(_PKG_DIR, "pyagy", in_pkg_rel, sibling_rel)


# The bundled pyagy/vendor/libagy.so (wheel) or the sibling antigravity/vendor/libagy.so
# (checkout) — build-id coupled to the vendor/agy host executable (same build).
LIBAGY = _vendored("vendor/libagy.so", "../vendor/libagy.so")
SHIM = LIBAGY


def linked_argv(agy_bin: str | os.PathLike[str], agy_args: Sequence[str],
                 env: MutableMapping[str, str] | None = None) -> list[str]:
    """Build the argv that runs ``agy_bin`` (linked directly against ``libagy.so``) and record
    ``AGY_PROC_REAL_EXE`` in ``env`` for the ``readlink_filter`` book."""
    env = os.environ if env is None else env
    env["AGY_PROC_REAL_EXE"] = os.path.abspath(agy_bin)
    return [os.fspath(agy_bin), *agy_args]


def linked_env(capture: str | os.PathLike[str] = "agy-capture.jsonl",
               log: str | os.PathLike[str] | None = None,
               module: str = "pyagy.agy_process",
               base: Mapping[str, str] | None = None,
               extra_env: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """Enable books and configure the embedded worker's module and JSONL capture.

    The interpreter finds pyagy/wirecap through site and PYTHONHOME. This adds no preload,
    library-path or PYTHONPATH settings. extra_env applies last; None removes a variable.
    """
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
        # install the os.OpenFile conversation-id probe (overlay) so linked runs learn
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
    home = python_home(env)
    if home:
        env.setdefault("PYTHONHOME", home)
    if log:
        env["AGY_PROC_LOG"] = os.path.abspath(log)
    for key, value in (extra_env or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    if any(env.get(key) not in (None, "", "0") for key in
           ("AGY_PROC_TLS_WRITE_SYNC", "AGY_PROC_REWRITE_RULES", "AGY_PROC_REWRITE")):
        raise NotImplementedError("agy request rewriting is unsupported by the current native trampoline")
    return env
