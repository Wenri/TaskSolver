#!/usr/bin/env bash
# Launch agy under the antigravity instrumentation.
#
#   test_scripts/run-agy.sh [agy args...]
#
# Env knobs (all optional):
#   AGY_BIN                 path to agy            (default: the pinned antigravity/vendor/agy)
#   AGY_PROC_CAPTURE        JSONL output           (default ./agy-capture.jsonl)
#   AGY_PROC_LOG            native shim log        (default ./antigravity.log)
#   AGY_PROC_TLS_WRITE_SYNC set to enable synchronous egress rewrite
#   AGY_PROC_H2             0 to disable HTTP/2 reassembly
#
# The env wiring (AGY_PROC_* + PYTHONPATH + GODEBUG) and the shim injection (agy's PT_INTERP +
# --preload, so the shim doesn't leak into agy's children via LD_PRELOAD) are delegated to
# pyagy._env.instrumented_env / preload_argv so this launcher and the Python drivers stay in sync.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANTIGRAVITY="$(cd "$HERE/../antigravity" && pwd)"   # the shim (vendor/) + python subsystem live here

: "${AGY_BIN:=$ANTIGRAVITY/vendor/agy}"   # the pinned, build-id-matched binary (instrumentation needs it)

exec python3 - "$AGY_BIN" "$@" <<PY
import os, sys
sys.path.insert(0, ${ANTIGRAVITY@Q})
from pyagy._env import instrumented_env, preload_argv
agy, *args = sys.argv[1:]
env = instrumented_env(
    capture=os.environ.get("AGY_PROC_CAPTURE", os.path.join(os.getcwd(), "agy-capture.jsonl")),
    log=os.environ.get("AGY_PROC_LOG", os.path.join(os.getcwd(), "antigravity.log")),
    module=os.environ.get("WIRE_MODULE", "pyagy.agy_process"),
    root=${ANTIGRAVITY@Q},
)
argv = preload_argv(agy, args, env=env)   # run agy through its PT_INTERP with --preload (no LD_PRELOAD)
os.execve(argv[0], argv, env)
PY
