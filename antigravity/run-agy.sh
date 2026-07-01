#!/usr/bin/env bash
# Launch agy under the antigravity instrumentation.
#
#   ./run-agy.sh [agy args...]
#
# Env knobs (all optional):
#   AGY_BIN                 path to agy            (default ~/.local/bin/agy)
#   AGY_HOOK_STAGE          1=python+DNS only (default), 2=+smoke hook,
#                           3=+tls_write+decrypt (request+response capture),
#                           4=serializer/proto R&D, 5=parking hooks (STALL agy)
#   AGY_HOOK_CAPTURE        JSONL output           (default ./agy-capture.jsonl)
#   AGY_HOOK_LOG            native shim log        (default ./antigravity.log)
#   AGY_HOOK_TLS_WRITE_SYNC set to enable synchronous egress rewrite
#   AGY_HOOK_H2             0 to disable HTTP/2 reassembly
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${AGY_BIN:=$HOME/.local/bin/agy}"
export AGY_HOOK_ENABLE=1
# AGY_HOOK_STAGE: 1=Python bridge + getaddrinfo DNS logging, no gum hooks (default).
#   3 = + tls_write hook (past-prologue): captures the model REQUEST in-process
#       (HTTP/2+JSON), no stall. Set AGY_HOOK_PREVIEW high to grab full bodies.
#   Stage 5 hooks (tls_read/RoundTrip) STALL agy (they park while hooked) — avoid.
# NOTE: agy needs a real git workspace (an empty dir hangs at startup).
export AGY_HOOK_STAGE="${AGY_HOOK_STAGE:-1}"
export AGY_HOOK_MODULE="${AGY_HOOK_MODULE:-agy_hooks}"
export AGY_HOOK_PYTHONPATH="${AGY_HOOK_PYTHONPATH:-$HERE/python}"
export AGY_HOOK_CAPTURE="${AGY_HOOK_CAPTURE:-$PWD/agy-capture.jsonl}"
export AGY_HOOK_LOG="${AGY_HOOK_LOG:-$PWD/antigravity.log}"
export PYTHONPATH="$HERE/python:${PYTHONPATH:-}"

# Force Go's cgo DNS resolver so the getaddrinfo interposer sees hostnames.
export GODEBUG="netdns=cgo${GODEBUG:+,$GODEBUG}"

exec env LD_PRELOAD="$HERE/build/antigravity.so${LD_PRELOAD:+:$LD_PRELOAD}" "$AGY_BIN" "$@"
