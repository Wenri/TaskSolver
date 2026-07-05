"""Environment wiring for an instrumented agy run: LD_PRELOAD the shim and wire the
in-process Python subsystem (``pyagy.agy_process``) + capture JSONL. Used by
:class:`pyagy.agyprocess.AgyProcess` (the single agy launcher) and test_scripts/run-agy.sh.
"""
import os

# antigravity/ — the dir holding vendor/antigravity.so and the pyagy package.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(ROOT, "vendor", "antigravity.so")


def _prepend(env, key, value, sep=os.pathsep):
    existing = env.get(key)
    env[key] = value + (sep + existing if existing else "")


def instrumented_env(capture="agy-capture.jsonl", log=None,
                     module="pyagy.agy_process", root=None, shim=None,
                     base=None, extra_env=None):
    """Environment that LD_PRELOADs the antigravity shim and points its embedded
    interpreter at ``module`` (default ``pyagy.agy_process``), writing hook events to
    the ``capture`` JSONL. The shim installs the full working hook union (wire + app +
    rpc) on every run — the only gate is ``AGY_PROC_ENABLE`` (set here).
    Mirrors run-agy.sh; ``extra_env`` (applied last) can override any AGY_PROC* knob."""
    root = root or ROOT
    shim = shim or os.path.join(root, "vendor", "antigravity.so")
    env = dict(base if base is not None else os.environ)
    env.update({
        "AGY_PROC_ENABLE": "1",
        "AGY_PROC_MODULE": module,
        "AGY_PROC_PYTHONPATH": root,
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
    _prepend(env, "LD_PRELOAD", shim)
    # The shim is linked with an RPATH to the build-time $CONDA_PREFIX/lib, but a pixi run
    # leaves LD_LIBRARY_PATH empty — add it so the LD_PRELOAD always resolves libpython
    # (else the preload fails with "libpython3.13.so: cannot open shared object").
    conda = env.get("CONDA_PREFIX")
    if conda:
        _prepend(env, "LD_LIBRARY_PATH", os.path.join(conda, "lib"))
    if log:
        env["AGY_PROC_LOG"] = os.path.abspath(log)
    if extra_env:
        env.update(extra_env)
    return env
