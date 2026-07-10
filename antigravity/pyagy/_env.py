"""Environment + argv wiring for an instrumented agy run: inject the antigravity shim via the
program interpreter's ``--preload`` (see :func:`preload_argv`) and wire the in-process Python
subsystem (``pyagy.agy_process``) + capture JSONL. Used by :class:`pyagy.agyprocess.AgyProcess`
(the single agy launcher) and test_scripts/run-agy.sh.

The shim is injected as a per-exec loader argument, NOT via ``LD_PRELOAD``: an env var is inherited
by every process agy spawns (a Playwright/Node helper, each ``bash -c`` tool command), each of which
then needlessly loads the shim — the reason the shim carries a build-id / ``_AGY_SBOXSERVE`` bail-out.
Running agy through its own ``PT_INTERP`` with ``--preload`` (+ ``--library-path`` for the shim's
libpython) scopes the injection to the one agy exec, leaving nothing shim-related in the environment.
"""
import os

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))   # .../pyagy


def _vendored(in_pkg_rel, sibling_rel):
    """Resolve a native artifact by layout. A self-contained wheel bundles it under the
    package (``pyagy/vendor/…``); a source/editable checkout keeps it in the sibling
    ``antigravity/vendor/…``. Prefer the in-package copy, fall back to the sibling; callers
    OR an explicit env override (AGY_SHIM/AGY_BIN) ahead of this."""
    in_pkg = os.path.join(_PKG_DIR, in_pkg_rel)
    return in_pkg if os.path.exists(in_pkg) else os.path.join(_PKG_DIR, sibling_rel)


# AGY_SHIM overrides the resolved shim path (dev escape hatch); otherwise the bundled
# pyagy/vendor/antigravity.so (wheel) or the sibling antigravity/vendor/antigravity.so
# (checkout). Must be the same build as the agy AGY_BIN/_VENDOR_AGY points at (build-id coupled).
SHIM = os.environ.get("AGY_SHIM") or _vendored("vendor/antigravity.so", "../vendor/antigravity.so")


def _elf_interp(path):
    """The program interpreter (``PT_INTERP``) baked into an ELF — the dynamic loader the kernel
    would use to run it (e.g. ``/lib64/ld-linux-x86-64.so.2``). We invoke that loader explicitly so
    ``--preload`` injects the shim for this one exec instead of via an inherited ``LD_PRELOAD``.
    Parsed with LIEF; raises if the binary has no interpreter (statically linked). LIEF is imported
    lazily so the shim's embedded interpreter — which imports only the stdlib-pure decode layer, not
    this launcher module — never needs it (only the parent-side launcher calls this)."""
    import lief
    binary = lief.parse(path)
    if binary is None:
        raise ValueError(f"{path}: not parseable as an ELF binary")
    if not binary.has_interpreter:
        raise ValueError(f"{path}: no PT_INTERP (statically linked?)")
    return binary.interpreter


def preload_argv(agy_bin, agy_args, shim=None, env=None):
    """Build the argv that runs ``agy_bin`` with the shim injected via its interpreter's
    ``--preload`` — a per-exec injection that (unlike ``LD_PRELOAD``) agy's children do not inherit.
    ``--argv0`` preserves agy's own ``argv[0]``; ``--library-path`` points the loader at
    ``$CONDA_PREFIX/lib`` (+ any existing ``LD_LIBRARY_PATH``) so the shim's libpython resolves
    without leaving ``LD_LIBRARY_PATH`` in the environment either."""
    shim = shim or SHIM
    env = os.environ if env is None else env
    # The shim's READLINK_FILTER hook returns this for os.readlink("/proc/self/exe") — which the
    # kernel resolves to the loader here (argv[0] is the interpreter, below), not agy.
    env["AGY_PROC_REAL_EXE"] = os.path.abspath(agy_bin)
    argv = [_elf_interp(agy_bin), "--argv0", agy_bin]
    libpath = [p for p in (os.path.join(env["CONDA_PREFIX"], "lib") if env.get("CONDA_PREFIX") else None,
                           env.get("LD_LIBRARY_PATH")) if p]
    if libpath:
        argv += ["--library-path", os.pathsep.join(libpath)]
    return argv + ["--preload", shim, agy_bin, *agy_args]


def instrumented_env(capture="agy-capture.jsonl", log=None,
                     module="pyagy.agy_process", root=None,
                     base=None, extra_env=None):
    """Environment for an instrumented agy run: points the shim's embedded interpreter at
    ``module`` (default ``pyagy.agy_process``) and writes hook events to the ``capture`` JSONL.
    The shim installs the full working hook union (wire + app + rpc) on every run — the only gate
    is ``AGY_PROC_ENABLE`` (set here). This sets NO ``LD_PRELOAD``/``LD_LIBRARY_PATH``/``PYTHONPATH``:
    the shim is injected via :func:`preload_argv` (the loader's ``--preload``/``--library-path``) and
    the embedded interpreter finds pyagy/wirecap via ``site`` (see ``PYTHONHOME`` below), so nothing
    shim-related leaks into agy's children. Mirrors run-agy.sh; ``extra_env`` (applied last) can
    override any AGY_PROC* knob."""
    del root  # kept for signature compatibility; sys.path is no longer injected
    env = dict(base if base is not None else os.environ)
    env.update({
        "AGY_PROC_ENABLE": "1",
        # WIRE_MODULE is the shared native bridge's contract (wirecap/native). The bridge no longer
        # takes a sys.path from us: Py_InitializeEx runs `site`, so the embedded interpreter imports
        # pyagy.agy_process (+ wirecap) from its own env's site-packages — the same install the parent
        # runs, pinned to that env by PYTHONHOME below. No PYTHONPATH is exported into agy's children.
        "WIRE_MODULE": module,
        "AGY_PROC_CAPTURE": os.path.abspath(capture),
        # install the os.OpenFile conversation-id probe (overlay) so instrumented runs learn
        # the exact conversation id in-process (agy doesn't expose it via env); mtime is the
        # fallback. The FILE_OPEN gum hook only attaches when this is set, and its C-side
        # path filter keeps it cheap (Python sees only conversation-store opens).
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
    if extra_env:
        env.update(extra_env)
    return env
