# antigravity — build the LD_PRELOAD shim (vendor/antigravity.so).
# Single entry point (folds the former setup.sh + src/build.sh). GNU make is a pixi
# dependency, so run it via `pixi run make -C antigravity` (this host has no system make).
#
#   make            # ensure deps (setup) + build the shim from the committed symbols.json
#   make symbols    # re-resolve symbols.json from vendor/agy (after an agy update), then `make`
#   make setup      # just vendor/fetch deps: agy binary (pinned, sha512-verified), frida-gum, UAPI headers
#   make agy-update # fetch the LATEST agy from the official manifest + print new pin values
#   make clean
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:

FRIDA_VER ?= 17.15.3
comma     := ,
# Prefer the conda C compiler over a bare PATH `gcc`. On a host with Nix on PATH, `gcc`
# resolves to Nix's gcc, which bakes /nix/store glibc rpaths into the shim — and that Nix
# glibc demands GLIBC_ABI_DT_X86_64_PLT, a symbol version the system libc lacks, so
# LD_PRELOADing the shim then fails before main (agy, even /bin/true). The conda compiler
# links against its old sysroot glibc and uses the SYSTEM libc at runtime (like agy) — no
# rpath leak, no version clash. Search the conda prefixes (build/host/runtime) for the
# wrapper; fall back to a PATH gcc only if none is present.
CONDA_CC  := $(firstword $(foreach p,$(BUILD_PREFIX) $(PREFIX) $(CONDA_PREFIX),$(wildcard $(p)/bin/x86_64-conda-linux-gnu-gcc)))
CC        ?= $(if $(CONDA_CC),$(CONDA_CC),gcc)

# Pinned agy release — the binary whose build-id matches the committed symbols.json.
# agy self-updates in the background, so a local install's build-id DRIFTS; we vendor a
# PINNED, sha512-verified release straight from the official CLI updater so the addresses
# in symbols.json (resolved from this exact binary) stay valid. Values come from
# $(AGY_MANIFEST_BASE)/manifests/linux_amd64.json — bump them with `make agy-update`,
# then `make symbols` and commit symbols.json + this pin together (build-id changes).
AGY_VERSION ?= 1.0.16
AGY_URL     ?= https://storage.googleapis.com/antigravity-public/antigravity-cli/1.0.16-4893150192467968/linux-x64/cli_linux_x64.tar.gz
AGY_SHA512  ?= 2e3362fb360d350285c3a79891772652ea56608121e4e89091abe553e8203c379e30bac86bde109ecc0b3266cb6eb871a68ef28a993aeca7944456f0f720ae31
AGY_MANIFEST_BASE ?= https://antigravity-cli-auto-updater-974169037036.us-central1.run.app
# Optional escape hatch: point at a local agy build to vendor instead of downloading the
# pin (it must match symbols.json's build-id or the shim's load-time guard degrades).
# Empty (default) = fetch the pinned release. NOTE: not $(HOME)/.local/bin/agy, which
# self-updates and would silently drift off the pinned symbols.
AGY_BIN   ?=
# WSL1's 4.4 kernel rejects tcmalloc's MAP_FIXED_NOREPLACE mmap flag (agy SIGABRTs before
# main), so a freshly-fetched agy must be byte-patched to run there — symbols/patch_agy_wsl1.py
# clears that flag in-place (address- & build-id-preserving, so symbols.json stays valid).
# Auto-detected from `uname -r` (WSL1 = "…-Microsoft"); real kernels/WSL2 need no patch.
# Override with AGY_WSL1=1/0.
AGY_WSL1  ?= $(shell uname -r | grep -q Microsoft && echo 1 || echo 0)

GUM  := vendor/frida-gum
# vendor/ is gitignored, so the built shim lives alongside its fetched deps.
SHIM := vendor/antigravity.so

# Linux UAPI headers (linux/, asm/): glibc's <limits.h> (pulled in via frida-gum.h)
# needs <linux/limits.h>, and this WSLackware host has none in /usr/include. Reference
# the conda kernel-headers sysroot (a declared pixi dep) in place via -idirafter — no
# copy. Pick the first of CONDA_PREFIX/PREFIX/BUILD_PREFIX (env vars make imports
# automatically) whose sysroot has the header; empty on a healthy host that already
# has /usr/include/linux, so -idirafter is then omitted entirely.
UAPI_REL := x86_64-conda-linux-gnu/sysroot/usr/include
UAPI_HIT := $(firstword $(foreach p,$(CONDA_PREFIX) $(PREFIX) $(BUILD_PREFIX),$(wildcard $(p)/$(UAPI_REL)/linux/limits.h)))
UAPI_INC := $(if $(wildcard /usr/include/linux/limits.h),,$(patsubst %/linux/limits.h,%,$(UAPI_HIT)))

# Embed the pixi env's libpython 3.13 — unified with the pixi-3.13 driver so the
# multiprocessing/pickle wire between them is same-version (AgyProcess). The RPATH
# below points the shim at $(CONDA_PREFIX)/lib so agy finds libpython3.13 at runtime
# (agy is always launched via pyagy/_env.py under the pixi env).
PYCFG   := $(shell command -v "$(CONDA_PREFIX)/bin/python3-config" 2>/dev/null || command -v python3-config || command -v /usr/bin/python3-config)
CFLAGS  := -O2 -g -fPIC -Wall -Wextra -std=gnu11 -fvisibility=hidden \
           -ffunction-sections -fdata-sections -I$(GUM) $(shell $(PYCFG) --includes) $(if $(UAPI_INC),-idirafter $(UAPI_INC))
LDFLAGS := -shared -Wl,--exclude-libs,ALL -Wl,--gc-sections $(if $(CONDA_PREFIX),-Wl$(comma)-rpath$(comma)$(CONDA_PREFIX)/lib,)
LIBS    := $(GUM)/libfrida-gum.a $(shell $(PYCFG) --ldflags --embed) -lrt -lresolv -ldl -lm -pthread

.PHONY: all setup symbols agy-update clean
all: $(SHIM)

# Recompile only when sources/symbols change; `| setup` (order-only) ensures the
# vendored deps exist first without forcing a rebuild.
$(SHIM): src/antigravity.c src/pybridge.c src/pybridge.h src/gomod.c src/cgotrampoline.c src/gomod.h src/cgotrampoline.h src/procdef.h symbols/gen_symbols_header.py symbols/symbols.json | setup
	test -n "$(UAPI_INC)" || test -f /usr/include/linux/limits.h || { echo "ERROR: no linux/limits.h — install kernel-headers_linux-64 (a pixi dep) so \$$CONDA_PREFIX has a sysroot (CONDA_PREFIX=$(CONDA_PREFIX) PREFIX=$(PREFIX))" >&2; exit 1; }
	echo "[build] UAPI headers: $(if $(UAPI_INC),$(UAPI_INC),/usr/include (system))"
	python3 symbols/gen_symbols_header.py symbols/symbols.json src/symbols_gen.h
	mkdir -p vendor
	$(CC) $(CFLAGS) $(LDFLAGS) -o $(SHIM) src/antigravity.c src/pybridge.c src/gomod.c src/cgotrampoline.c $(LIBS)
	echo "built $(SHIM) ($$(stat -c%s $(SHIM)) bytes)"

# Vendor the deps that are NOT committed (see .gitignore); all steps idempotent.
setup:
	if [ -f vendor/agy ]; then echo "[setup] agy present ($$(readelf -n vendor/agy 2>/dev/null | awk '/Build ID/{print $$NF}'))"
	elif [ -n "$(AGY_BIN)" ] && [ -f "$(AGY_BIN)" ]; then
	  mkdir -p vendor && cp "$(AGY_BIN)" vendor/agy && echo "[setup] vendored agy from $(AGY_BIN)"
	  if [ "$(AGY_WSL1)" = 1 ]; then python3 symbols/patch_agy_wsl1.py vendor/agy || echo "[setup] WSL1 patch skipped (source already patched?)"; fi
	else
	  echo "[setup] fetching pinned agy $(AGY_VERSION) (sha512-verified) from the official CLI release"
	  mkdir -p vendor
	  tmp="$$(mktemp -t agy.XXXXXX.tar.gz)"
	  curl -fsSL --proto '=https' --tlsv1.2 --max-time 300 "$(AGY_URL)" -o "$$tmp"
	  printf '%s  %s\n' "$(AGY_SHA512)" "$$tmp" | sha512sum -c - >/dev/null || { rm -f "$$tmp"; echo "[setup] FATAL: agy sha512 mismatch — refusing to vendor a corrupt/tampered binary" >&2; exit 1; }
	  tar -xzf "$$tmp" -C vendor antigravity && mv -f vendor/antigravity vendor/agy && chmod +x vendor/agy
	  rm -f "$$tmp"
	  echo "[setup] vendored agy $(AGY_VERSION) ($$(readelf -n vendor/agy 2>/dev/null | awk '/Build ID/{print $$NF}'))"
	  if [ "$(AGY_WSL1)" = 1 ]; then python3 symbols/patch_agy_wsl1.py vendor/agy; fi
	fi
	if [ -f "$(GUM)/libfrida-gum.a" ]; then echo "[setup] frida-gum present ($$(cat $(GUM)/VERSION 2>/dev/null))"
	else
	  echo "[setup] fetching frida-gum devkit $(FRIDA_VER) (GitHub-only; not on conda-forge/PyPI)"
	  mkdir -p "$(GUM)"
	  url="https://github.com/frida/frida/releases/download/$(FRIDA_VER)/frida-gum-devkit-$(FRIDA_VER)-linux-x86_64.tar.xz"
	  tmp="$$(mktemp -t gumdevkit.XXXXXX.tar.xz)"; trap 'rm -f "$$tmp"' EXIT
	  curl -fsSL --proto '=https' --tlsv1.2 --max-time 180 "$$url" -o "$$tmp"
	  tar -xf "$$tmp" -C "$(GUM)"
	  echo "$(FRIDA_VER)" > "$(GUM)/VERSION"
	fi
	# UAPI headers are NOT vendored — the build references the conda kernel-headers
	# sysroot in place via -idirafter (see UAPI_INC above).

# Re-resolve hook addresses from the current agy binary (after an agy update).
symbols: | setup
	python3 symbols/build_symbols.py vendor/agy symbols/symbols.json

clean:
	rm -f $(SHIM) src/symbols_gen.h

# Fetch the LATEST agy from the official updater manifest, vendor it (sha512-verified),
# and print the new pin values to paste above. The build-id WILL change vs the current
# pin, so follow with `make symbols` and commit symbols.json + the updated pin together.
# (Reproducible builds use the pinned AGY_URL above; this target is the deliberate bump.)
agy-update:
	platform="linux_$$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')"
	echo "[agy-update] querying $(AGY_MANIFEST_BASE)/manifests/$$platform.json"
	json="$$(curl -fsSL --max-time 60 "$(AGY_MANIFEST_BASE)/manifests/$$platform.json")"
	ver="$$(printf '%s' "$$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
	url="$$(printf '%s' "$$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')"
	sha="$$(printf '%s' "$$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha512"])')"
	echo "[agy-update] latest = $$ver"
	tmp="$$(mktemp -t agy.XXXXXX.tar.gz)"
	curl -fsSL --max-time 300 "$$url" -o "$$tmp"
	printf '%s  %s\n' "$$sha" "$$tmp" | sha512sum -c - >/dev/null || { rm -f "$$tmp"; echo "[agy-update] FATAL: sha512 mismatch" >&2; exit 1; }
	mkdir -p vendor && tar -xzf "$$tmp" -C vendor antigravity && mv -f vendor/antigravity vendor/agy && chmod +x vendor/agy
	rm -f "$$tmp"
	if [ "$(AGY_WSL1)" = 1 ]; then python3 symbols/patch_agy_wsl1.py vendor/agy; fi
	echo "[agy-update] vendored agy $$ver ($$(readelf -n vendor/agy 2>/dev/null | awk '/Build ID/{print $$NF}')). Update the pin above:"
	echo "    AGY_VERSION ?= $$ver"
	echo "    AGY_URL     ?= $$url"
	echo "    AGY_SHA512  ?= $$sha"
	echo "[agy-update] then: pixi run make -C antigravity symbols   (re-resolve + commit symbols.json + Makefile)"
