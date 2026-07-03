"""Environment wiring shared by every agy launcher.

Two shapes of environment, previously copied across `session._clean_env`,
`test_scripts/agy_session.AgySession._env`, and `test_scripts/run-agy.sh`:

  * ``clean_env()``        — a *non*-instrumented run: strip our AGY_PROC* knobs and
                             remove the antigravity.so entry from LD_PRELOAD.
  * ``instrumented_env()`` — LD_PRELOAD the shim and wire the in-process Python
                             subsystem (pyagy.agy_process) + capture JSONL.
"""
import os

# antigravity/ — the dir holding vendor/antigravity.so and the pyagy package.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(ROOT, "vendor", "antigravity.so")


def _prepend(env, key, value, sep=os.pathsep):
    existing = env.get(key)
    env[key] = value + (sep + existing if existing else "")


def clean_env(base=None):
    """Environment for a plain (uninstrumented) agy run: drops every AGY_PROC* knob
    and removes only *our* antigravity.so from LD_PRELOAD (any other preload is kept),
    and guarantees TERM is set."""
    env = dict(base if base is not None else os.environ)
    env["TERM"] = env.get("TERM", "xterm-256color")
    for k in list(env):
        if k.startswith("AGY_PROC"):
            env.pop(k, None)
    kept = [p for p in env.get("LD_PRELOAD", "").split(os.pathsep)
            if p and "antigravity" not in p]
    if kept:
        env["LD_PRELOAD"] = os.pathsep.join(kept)
    else:
        env.pop("LD_PRELOAD", None)
    # Disable agy's background self-update: it re-downloads the binary, drifting its
    # build-id off the pinned symbols.json and undoing the WSL1 patch. `true` per the
    # official CLI troubleshooting guide.
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return env


def instrumented_env(stage=3, capture="agy-capture.jsonl", log=None,
                     module="pyagy.agy_process", root=None, shim=None,
                     base=None, extra_env=None):
    """Environment that LD_PRELOADs the antigravity shim and points its embedded
    interpreter at ``module`` (default ``pyagy.agy_process``), writing hook events to
    the ``capture`` JSONL. Mirrors run-agy.sh / the old AgySession._env; ``extra_env``
    (applied last) can override any of the AGY_PROC* knobs."""
    root = root or ROOT
    shim = shim or os.path.join(root, "vendor", "antigravity.so")
    env = dict(base if base is not None else os.environ)
    env.update({
        "AGY_PROC_ENABLE": "1",
        "AGY_PROC_STAGE": str(stage),
        "AGY_PROC_MODULE": module,
        "AGY_PROC_PYTHONPATH": root,
        "AGY_PROC_CAPTURE": os.path.abspath(capture),
        "GODEBUG": ("netdns=cgo," + env.get("GODEBUG", "")).rstrip(","),
        "TERM": env.get("TERM", "xterm-256color"),
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",   # disable agy's background self-update
    })
    _prepend(env, "PYTHONPATH", root)
    _prepend(env, "LD_PRELOAD", shim)
    if log:
        env["AGY_PROC_LOG"] = os.path.abspath(log)
    if extra_env:
        env.update(extra_env)
    return env
