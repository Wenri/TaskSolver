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

# antigravity/ — the dir holding vendor/antigravity.so and the pyagy package.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(ROOT, "vendor", "antigravity.so")


def _prepend(env, key, value, sep=os.pathsep):
    existing = env.get(key)
    env[key] = value + (sep + existing if existing else "")


def _elf_interp(path):
    """The program interpreter (``PT_INTERP``) baked into an ELF — the dynamic loader the kernel
    would use to run it (e.g. ``/lib64/ld-linux-x86-64.so.2``). We invoke that loader explicitly so
    ``--preload`` injects the shim for this one exec instead of via an inherited ``LD_PRELOAD``.
    Parsed with LIEF; raises if the binary has no interpreter (statically linked). LIEF is imported
    lazily so the shim's embedded interpreter — which imports ``pyagy.agy_process`` under ``-S``,
    with no site-packages on the path — never needs it (only the parent-side launcher calls this)."""
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
    is ``AGY_PROC_ENABLE`` (set here). This sets NO ``LD_PRELOAD``/``LD_LIBRARY_PATH``: the shim is
    injected via :func:`preload_argv` (the loader's ``--preload``/``--library-path``) so nothing
    shim-related leaks into agy's children. Mirrors run-agy.sh; ``extra_env`` (applied last) can
    override any AGY_PROC* knob."""
    root = root or ROOT
    env = dict(base if base is not None else os.environ)
    env.update({
        "AGY_PROC_ENABLE": "1",
        # WIRE_MODULE / WIRE_PYTHONPATH are the shared native bridge's contract (wirecap/native).
        # The bridge splits WIRE_PYTHONPATH on os.pathsep and inserts each root — the shared
        # `wirecap` package lives at the repo root, the `pyagy` package under antigravity/, so the
        # embedded interpreter needs BOTH roots to import pyagy.agy_process (which imports wirecap).
        "WIRE_MODULE": module,
        "WIRE_PYTHONPATH": os.path.dirname(root) + os.pathsep + root,
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
    _prepend(env, "PYTHONPATH", root)
    if log:
        env["AGY_PROC_LOG"] = os.path.abspath(log)
    if extra_env:
        env.update(extra_env)
    return env
