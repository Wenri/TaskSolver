#!/usr/bin/env bash
# Fetch the LATEST agy from the official updater manifest, vendor it (sha512-verified), and print
# the new pin values to paste into antigravity/CMakeLists.txt. The build-id WILL change vs the
# current pin, so follow with `pixi run shim-symbols` and commit symbols.json + CMakeLists.txt
# together. (Reproducible builds use the pinned AGY_URL in CMakeLists.txt; this is the deliberate
# bump.) Invoked by the CMake `agy-update` target — `pixi run shim-agy-update`.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # antigravity/

AGY_MANIFEST_BASE="${AGY_MANIFEST_BASE:-https://antigravity-cli-auto-updater-974169037036.us-central1.run.app}"
AGY_WSL1="${AGY_WSL1:-$(uname -r | grep -q Microsoft && echo 1 || echo 0)}"

platform="linux_$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')"
echo "[agy-update] querying $AGY_MANIFEST_BASE/manifests/$platform.json"
json="$(curl -fsSL --max-time 60 "$AGY_MANIFEST_BASE/manifests/$platform.json")"
ver="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
url="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')"
sha="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha512"])')"
echo "[agy-update] latest = $ver"
tmp="$(mktemp -t agy.XXXXXX.tar.gz)"
curl -fsSL --max-time 300 "$url" -o "$tmp"
printf '%s  %s\n' "$sha" "$tmp" | sha512sum -c - >/dev/null \
  || { rm -f "$tmp"; echo "[agy-update] FATAL: sha512 mismatch" >&2; exit 1; }
mkdir -p vendor && tar -xzf "$tmp" -C vendor antigravity && mv -f vendor/antigravity vendor/agy && chmod +x vendor/agy
rm -f "$tmp"
if [ "$AGY_WSL1" = 1 ]; then python3 symbols/patch_agy_wsl1.py vendor/agy; fi
echo "[agy-update] vendored agy $ver ($(readelf -n vendor/agy 2>/dev/null | awk '/Build ID/{print $NF}')). Update the pins in CMakeLists.txt:"
echo "    set(AGY_VERSION \"$ver\")"
echo "    set(AGY_URL     \"$url\")"
echo "    set(AGY_SHA512  \"$sha\")"
echo "[agy-update] then: pixi run shim-symbols   (re-resolve + commit symbols.json + CMakeLists.txt)"
