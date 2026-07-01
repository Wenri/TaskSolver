# antigravity — build the LD_PRELOAD shim (vendor/antigravity.so).
# Single entry point (folds the former setup.sh + src/build.sh). GNU make is a pixi
# dependency, so run it via `pixi run make -C antigravity` (this host has no system make).
#
#   make            # ensure deps (setup) + build the shim from the committed symbols.json
#   make symbols    # re-resolve symbols.json from vendor/agy (after an agy update), then `make`
#   make setup      # just vendor/fetch deps: agy binary, frida-gum devkit, UAPI headers
#   make clean
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:

FRIDA_VER ?= 17.15.3
AGY_BIN   ?= $(HOME)/.local/bin/agy
CC        ?= gcc

GUM  := vendor/frida-gum
UAPI := vendor/uapi
# vendor/ is gitignored, so the built shim lives alongside its fetched deps.
SHIM := vendor/antigravity.so

# Embed the SYSTEM libpython — the shim is LD_PRELOADed into agy run standalone, so
# it must need a libpython that's always on the loader path (not a pixi env's).
PYCFG   := $(shell command -v /usr/bin/python3-config || command -v python3-config)
CFLAGS  := -O2 -g -fPIC -Wall -Wextra -std=gnu11 -fvisibility=hidden \
           -ffunction-sections -fdata-sections -I$(GUM) $(shell $(PYCFG) --includes) -idirafter $(UAPI)
LDFLAGS := -shared -Wl,--exclude-libs,ALL -Wl,--gc-sections
LIBS    := $(GUM)/libfrida-gum.a $(shell $(PYCFG) --ldflags --embed) -lrt -lresolv -ldl -lm -pthread

.PHONY: all setup symbols clean
all: $(SHIM)

# Recompile only when sources/symbols change; `| setup` (order-only) ensures the
# vendored deps exist first without forcing a rebuild.
$(SHIM): src/antigravity.c src/pybridge.c src/pybridge.h src/hooks.def src/gen_symbols_header.py symbols/symbols.json | setup
	python3 src/gen_symbols_header.py symbols/symbols.json src/symbols_gen.h
	mkdir -p vendor
	$(CC) $(CFLAGS) $(LDFLAGS) -o $(SHIM) src/antigravity.c src/pybridge.c $(LIBS)
	echo "built $(SHIM) ($$(stat -c%s $(SHIM)) bytes)"

# Vendor the deps that are NOT committed (see .gitignore); all steps idempotent.
setup:
	if [ -f vendor/agy ]; then echo "[setup] agy present"
	elif [ -f "$(AGY_BIN)" ]; then mkdir -p vendor && cp "$(AGY_BIN)" vendor/agy && echo "[setup] vendored agy from $(AGY_BIN)"
	else echo "[setup] WARN: $(AGY_BIN) not found — set AGY_BIN or copy to vendor/agy" >&2
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
	if [ -f "$(UAPI)/linux/limits.h" ] && [ -f "$(UAPI)/asm/socket.h" ]; then echo "[setup] UAPI headers present"
	elif [ -f /usr/include/linux/limits.h ] && [ -f /usr/include/asm/socket.h ]; then echo "[setup] system UAPI headers present"
	else
	  echo "[setup] vendoring UAPI headers from a conda kernel-headers sysroot (this host lacks linux/+asm/)"
	  src=""
	  for c in "$(HOME)"/Git/*/.pixi/envs/*/x86_64-conda-linux-gnu/sysroot/usr/include \
	           "$(HOME)"/.cache/rattler/cache/pkgs/kernel-headers_linux-64-*/x86_64-conda-linux-gnu/sysroot/usr/include \
	           /opt/conda/x86_64-conda-linux-gnu/sysroot/usr/include; do
	    if [ -f "$$c/linux/limits.h" ] && [ -f "$$c/asm/socket.h" ]; then src="$$c"; break; fi
	  done
	  [ -n "$$src" ] || { echo "[setup] ERROR: no UAPI header source found (install kernel headers)" >&2; exit 1; }
	  mkdir -p "$(UAPI)"
	  for d in linux asm asm-generic mtd misc rdma sound scsi drm xen video; do
	    [ -d "$$src/$$d" ] && cp -r "$$src/$$d" "$(UAPI)/" 2>/dev/null || true
	  done
	  echo "[setup] UAPI headers vendored from $$src"
	fi

# Re-resolve hook addresses from the current agy binary (after an agy update).
symbols: | setup
	python3 symbols/build_symbols.py vendor/agy symbols/symbols.json

clean:
	rm -f $(SHIM) src/symbols_gen.h
