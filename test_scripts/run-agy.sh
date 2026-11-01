#!/usr/bin/env bash
# Launch the linked agy host with capture enabled.
#
#   test_scripts/run-agy.sh [agy args...]
#
# Env knobs (all optional):
#   AGY_BIN                 path to agy            (default: the pinned antigravity/vendor/agy)
#   WIRE_CAPTURE            JSONL output           (default ./agy-capture.jsonl)
#                           (legacy AGY_PROC_CAPTURE still accepted)
#   AGY_PROC_LOG            native shim log        (default ./antigravity.log)
#   AGY_PROC_TLS_WRITE_SYNC set to enable synchronous egress rewrite
#   AGY_PROC_H2             0 to disable HTTP/2 reassembly
#
# Launch configuration is shared with the Python drivers in pyagy._env.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANTIGRAVITY="$(cd "$HERE/../antigravity" && pwd)"   # the shim (vendor/) + python subsystem live here

: "${AGY_BIN:=$ANTIGRAVITY/vendor/agy}"   # the pinned, build-id-matched binary (matches libagy.so)

exec python3 - "$AGY_BIN" "$@" <<PY
import os, sys
sys.path.insert(0, ${ANTIGRAVITY@Q})
from pyagy._env import linked_env, linked_argv
agy, *args = sys.argv[1:]
env = linked_env(
    capture=os.environ.get("WIRE_CAPTURE",
                           os.environ.get("AGY_PROC_CAPTURE",
                                          os.path.join(os.getcwd(), "agy-capture.jsonl"))),
    log=os.environ.get("AGY_PROC_LOG", os.path.join(os.getcwd(), "antigravity.log")),
    module=os.environ.get("WIRE_MODULE", "pyagy.agy_process"),
)
argv = linked_argv(agy, args, env=env)
os.execve(argv[0], argv, env)
PY
