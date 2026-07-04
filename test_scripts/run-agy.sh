#!/usr/bin/env bash
# Launch agy under the antigravity instrumentation.
#
#   test_scripts/run-agy.sh [agy args...]
#
# Env knobs (all optional):
#   AGY_BIN                 path to agy            (default ~/.local/bin/agy)
#   AGY_PROC_NOHOOK         set to run bridge-only (no capture hooks); default installs
#                           the full working hook union (wire + app + rpc)
#   AGY_PROC_CAPTURE        JSONL output           (default ./agy-capture.jsonl)
#   AGY_PROC_LOG            native shim log        (default ./antigravity.log)
#   AGY_PROC_TLS_WRITE_SYNC set to enable synchronous egress rewrite
#   AGY_PROC_H2             0 to disable HTTP/2 reassembly
#
# The env wiring (LD_PRELOAD + AGY_PROC_* + PYTHONPATH + GODEBUG) is delegated to
# pyagy._env.instrumented_env so this launcher and the Python drivers stay in sync.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANTIGRAVITY="$(cd "$HERE/../antigravity" && pwd)"   # the shim (vendor/) + python subsystem live here

: "${AGY_BIN:=$HOME/.local/bin/agy}"

exec python3 - "$AGY_BIN" "$@" <<PY
import os, sys
sys.path.insert(0, ${ANTIGRAVITY@Q})
from pyagy._env import instrumented_env
agy, *args = sys.argv[1:]
env = instrumented_env(
    capture=os.environ.get("AGY_PROC_CAPTURE", os.path.join(os.getcwd(), "agy-capture.jsonl")),
    log=os.environ.get("AGY_PROC_LOG", os.path.join(os.getcwd(), "antigravity.log")),
    module=os.environ.get("AGY_PROC_MODULE", "pyagy.agy_process"),
    root=${ANTIGRAVITY@Q},
)
os.execve(agy, [agy] + args, env)
PY
