#!/usr/bin/env bash
# Build agyhook.so without `make` (this host has no make). Equivalent to the Makefile.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

[ -f vendor/frida-gum/libfrida-gum.a ] || { echo "run ../setup.sh first (frida-gum missing)"; exit 1; }
python3 gen_symbols_header.py ../symbols/symbols.json symbols_gen.h
mkdir -p ../build

# Embed the SYSTEM libpython, not a pixi env's — the shim is LD_PRELOADed into agy
# run standalone, so it must need a libpython that's always on the loader path.
# (Prefer /usr/bin/python3-config even when this runs under `pixi run`.)
PYCFG="$(command -v /usr/bin/python3-config || command -v python3-config)"
PYINC="$("$PYCFG" --includes)"
PYLD="$("$PYCFG" --ldflags --embed)"
UAPI=""; [ -d vendor/uapi ] && UAPI="-idirafter vendor/uapi"

# shellcheck disable=SC2086
${CC:-gcc} -O2 -g -fPIC -Wall -Wextra -std=gnu11 \
    -fvisibility=hidden -ffunction-sections -fdata-sections \
    -Ivendor/frida-gum $PYINC $UAPI \
    -shared -Wl,--exclude-libs,ALL -Wl,--gc-sections \
    -o ../build/agyhook.so agyhook.c pybridge.c \
    vendor/frida-gum/libfrida-gum.a $PYLD -lrt -lresolv -ldl -lm -pthread

echo "built ../build/agyhook.so ($(stat -c%s ../build/agyhook.so) bytes)"
