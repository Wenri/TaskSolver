#!/usr/bin/env bash
# Fetch/recreate the external build deps that are NOT committed (see .gitignore):
#   1. frida-gum devkit  (static libfrida-gum.a + header) — downloaded from GitHub
#   2. Linux UAPI headers (linux/, asm/, asm-generic/) — this host lacks them;
#      copied from a conda kernel-headers sysroot so the build is self-contained.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRIDA_VER="${FRIDA_VER:-17.15.3}"

# --- 0. vendor the agy binary (gitignored copy the tooling reads) ----------
AGY_SRC="${AGY_BIN:-$HOME/.local/bin/agy}"
if [ -f "$HERE/vendor/agy" ]; then
    echo "[setup] vendored agy present"
elif [ -f "$AGY_SRC" ]; then
    mkdir -p "$HERE/vendor"; cp "$AGY_SRC" "$HERE/vendor/agy"
    echo "[setup] vendored agy from $AGY_SRC"
else
    echo "[setup] WARN: $AGY_SRC not found — set AGY_BIN or copy agy to $HERE/vendor/agy" >&2
fi

# --- 1. frida-gum devkit (GitHub-only; not on conda-forge/PyPI) ------------
GUM="$HERE/vendor/frida-gum"
if [ -f "$GUM/libfrida-gum.a" ]; then
    echo "[setup] frida-gum devkit present ($(cat "$GUM/VERSION" 2>/dev/null))"
else
    echo "[setup] downloading frida-gum devkit $FRIDA_VER ..."
    mkdir -p "$GUM"
    url="https://github.com/frida/frida/releases/download/${FRIDA_VER}/frida-gum-devkit-${FRIDA_VER}-linux-x86_64.tar.xz"
    tmp="$(mktemp -t gumdevkit.XXXXXX.tar.xz)"
    trap 'rm -f "$tmp"' EXIT
    # HTTPS-only, no redirects to other schemes
    curl -fsSL --proto '=https' --tlsv1.2 --max-time 180 "$url" -o "$tmp"
    tar -xf "$tmp" -C "$GUM"
    rm -f "$tmp"; trap - EXIT
    echo "$FRIDA_VER" > "$GUM/VERSION"
    echo "[setup] frida-gum devkit ready"
fi

# --- 2. Linux UAPI headers -------------------------------------------------
UAPI="$HERE/vendor/uapi"
if [ -f "$UAPI/linux/limits.h" ] && [ -f "$UAPI/asm/socket.h" ]; then
    echo "[setup] UAPI headers present"
elif [ -f /usr/include/linux/limits.h ] && [ -f /usr/include/asm/socket.h ]; then
    echo "[setup] system UAPI headers found; vendoring not needed (edit Makefile to drop -idirafter)"
else
    echo "[setup] system lacks UAPI headers; vendoring from a conda kernel-headers sysroot ..."
    src=""
    for c in \
        "$HOME"/Git/*/.pixi/envs/*/x86_64-conda-linux-gnu/sysroot/usr/include \
        "$HOME"/.cache/rattler/cache/pkgs/kernel-headers_linux-64-*/x86_64-conda-linux-gnu/sysroot/usr/include \
        /opt/conda/x86_64-conda-linux-gnu/sysroot/usr/include ; do
        if [ -f "$c/linux/limits.h" ] && [ -f "$c/asm/socket.h" ]; then src="$c"; break; fi
    done
    if [ -z "$src" ]; then
        echo "[setup] ERROR: no UAPI header source found. Install kernel headers, or set" >&2
        echo "        -idirafter <sysroot>/usr/include in src/Makefile CFLAGS." >&2
        exit 1
    fi
    mkdir -p "$UAPI"
    for d in linux asm asm-generic mtd misc rdma sound scsi drm xen video; do
        [ -d "$src/$d" ] && cp -r "$src/$d" "$UAPI/" 2>/dev/null || true
    done
    echo "[setup] UAPI headers vendored from $src"
fi

echo "[setup] done. Next: python3 symbols/build_symbols.py vendor/agy symbols/symbols.json && (cd src && make)"
