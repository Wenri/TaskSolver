# agyhook native shim: LD_PRELOAD .so embedding frida-gum + libpython.
FRIDA     := vendor/frida-gum
SYMJSON   := ../symbols/symbols.json
AGY_BIN   := ../vendor/agy
OUT       := ../build/agyhook.so

PYCFLAGS  := $(shell python3-config --includes)
PYLDFLAGS := $(shell python3-config --ldflags --embed)

# This host (WSLackware) lacks Linux UAPI headers; vendor/uapi supplies them via
# -idirafter (lowest priority, so it only fills gaps and never shadows glibc).
CFLAGS  := -O2 -g -fPIC -Wall -Wextra -std=gnu11 \
           -fvisibility=hidden -ffunction-sections -fdata-sections \
           -I$(FRIDA) $(PYCFLAGS) -idirafter vendor/uapi
LDFLAGS := -shared -Wl,--exclude-libs,ALL -Wl,--gc-sections
LIBS    := $(FRIDA)/libfrida-gum.a $(PYLDFLAGS) -lrt -lresolv -ldl -lm -pthread

.PHONY: all symbols clean
all: $(OUT)

symbols_gen.h: $(SYMJSON) gen_symbols_header.py
	python3 gen_symbols_header.py $(SYMJSON) symbols_gen.h

$(OUT): agyhook.c pybridge.c pybridge.h hooks.def symbols_gen.h
	@mkdir -p ../build
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ agyhook.c pybridge.c $(LIBS)
	@echo "built $@"; ls -la $@

# Regenerate symbols.json from the vendored binary (after any agy update).
symbols:
	python3 ../symbols/build_symbols.py $(AGY_BIN) $(SYMJSON)

clean:
	rm -f symbols_gen.h $(OUT)
