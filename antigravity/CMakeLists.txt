# antigravity — build `vendor/libagy.so` and the linked host executable (`vendor/agy`).
#
# Configure + build (both done by setup.py's build_py hook during `pixi install`, and by the
# `build-shim` pixi task):
#   cmake -S antigravity -B antigravity/build -G Ninja
#   cmake --build antigravity/build
# Maintenance targets (see the pixi tasks shim-symbols / shim-agy-update):
#   cmake --build antigravity/build --target symbols     # ensure libagy.so + metadata match cached raw agy
#   cmake --build antigravity/build --target agy-update   # query the latest release + print new pins
#
# Configure caches the pinned raw agy (sha512-verified). The build converts it, records metadata
# before patching, and compiles the linked host.
cmake_minimum_required(VERSION 3.19)
project(antigravity CXX)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)   # for clangd (symlinked to the source root below)

# The shim MUST link against the conda toolchain's glibc, not a Nix gcc on PATH: Nix's gcc bakes
# /nix/store glibc rpaths that demand GLIBC_ABI_DT_X86_64_PLT (a symbol version the system libc
# lacks), so LD_PRELOADing the shim then fails before main. The pixi/conda env exports
# CXX=x86_64-conda-linux-gnu-c++, which CMake auto-selects on first configure — so configure INSIDE
# the pixi env. Warn if we somehow didn't get the conda compiler.
if(NOT CMAKE_CXX_COMPILER MATCHES "conda")
  message(WARNING "CMAKE_CXX_COMPILER='${CMAKE_CXX_COMPILER}' is not a conda wrapper — on a Nix host "
                  "this bakes /nix glibc rpaths that break the LD_PRELOAD. Configure inside the pixi env.")
endif()

set(CONDA_PREFIX "$ENV{CONDA_PREFIX}")
if(NOT CONDA_PREFIX)
  message(FATAL_ERROR "CONDA_PREFIX is not set — configure inside the pixi env (pixi run build-shim).")
endif()

# ---- pinned agy release (keep these three in sync; then `pixi run shim-symbols`) ---------------
# agy self-updates in the background, so a local install's build-id DRIFTS; we vendor a PINNED,
# sha512-verified release and derive symbols.json from its converted layout. AGY_BIN is an escape
# hatch: point it at a local raw agy build instead of downloading.
#
# The exact release URL and archive hash are pinned below; the updater manifest is consulted only
# by the explicit agy-update target. The tarball member is `antigravity` (extracted below).
#
# Version history worth keeping: 1.1.6 (2026-07-24) added a client-side eligibility check that
# hard-exited with "not currently available in your location" before any model call, which forced the
# pin down to 1.1.5 for a while (bisected 1.1.1→1.1.8). That gate no longer fires as of 1.1.9
# (2026-07-31) — so if a future release starts refusing to run, check for that message before
# assuming the shim broke.
#
# NOTE the pin drifts on its own: agy's background self-updater REPLACES vendor/agy in place (it did
# so mid-session on 2026-08-05, swapping 1.1.9 for 1.1.10 and breaking the build-id match), and
# AGY_PROC_* setting AGY_CLI_DISABLE_AUTO_UPDATE=true no longer stops it. Symptom is the shim logging
# "build-id not agy (running=… symbols=…); not hooking this process" and every hook silently not
# firing. Rebuild the host (rm vendor/agy && pixi run build-shim); never use vendor/agy as
# conversion input. `chmod a-w vendor/agy` blunts the in-place overwrite.
set(AGY_VERSION "1.4.11")
set(AGY_URL "https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.11-5180209989812224/linux-x64/cli_linux_x64.tar.gz")
set(AGY_SHA512 "2935601efa745ed6e314d32b415c71f44d9d455f0e6a2583372584be5a9074c28154cf81d5eec65b4416579453a3b3b05d3abe4d4f176754d5ef93a84af6cf28")
set(AGY_BIN "" CACHE STRING "optional: local raw agy input (relative to the source directory)")
# WSL1's 4.4 kernel rejects tcmalloc's MAP_FIXED_NOREPLACE mmap flag, so a freshly-fetched agy must
# be byte-patched to run there (length-preserving, so addresses and the build-id survive).
# make_libagy.py detects WSL1 and patches as part of the conversion; -DAGY_WSL1=0/1 forces it.
set(AGY_CONVERT_ENV)
if(DEFINED AGY_WSL1)
  list(APPEND AGY_CONVERT_ENV "AGY_WSL1=${AGY_WSL1}")
endif()

file(MAKE_DIRECTORY "${CMAKE_SOURCE_DIR}/vendor")

# ---- immutable raw input, keyed by the verified archive hash -------------------------------
if(AGY_BIN)
  get_filename_component(AGY_RAW "${AGY_BIN}" ABSOLUTE BASE_DIR "${CMAKE_SOURCE_DIR}")
  if(NOT EXISTS "${AGY_RAW}")
    message(FATAL_ERROR "AGY_BIN does not exist: ${AGY_RAW}")
  endif()
else()
  set(AGY_RAW "${CMAKE_BINARY_DIR}/antigravity-${AGY_SHA512}.raw")
endif()

if(NOT EXISTS "${AGY_RAW}")
  message(STATUS "[setup] fetching pinned agy ${AGY_VERSION} (sha512-verified)")
  set(_agy_tgz "${CMAKE_BINARY_DIR}/agy.tar.gz")
  file(DOWNLOAD "${AGY_URL}" "${_agy_tgz}" EXPECTED_HASH "SHA512=${AGY_SHA512}" STATUS _agy_st SHOW_PROGRESS)
  list(GET _agy_st 0 _agy_rc)
  if(NOT _agy_rc EQUAL 0)
    file(REMOVE "${_agy_tgz}")
    message(FATAL_ERROR "agy download/verify failed: ${_agy_st}")
  endif()
  execute_process(COMMAND tar -xzf "${_agy_tgz}" -C "${CMAKE_BINARY_DIR}" antigravity RESULT_VARIABLE _agy_tar)
  file(REMOVE "${_agy_tgz}")
  if(NOT _agy_tar EQUAL 0)
    file(REMOVE "${CMAKE_BINARY_DIR}/antigravity")
    message(FATAL_ERROR "agy extract failed")
  endif()
  file(RENAME "${CMAKE_BINARY_DIR}/antigravity" "${AGY_RAW}")
endif()
message(STATUS "[setup] raw agy input: ${AGY_RAW}")

# One conversion owns the library and its metadata. Read the final ELF layout before patching;
# every rebuild starts from unchanged raw input.
add_custom_command(
  OUTPUT "${CMAKE_SOURCE_DIR}/vendor/libagy.so"
         "${CMAKE_SOURCE_DIR}/symbols/symbols.json"
         "${CMAKE_SOURCE_DIR}/symbols/funcmap.tsv.gz"
  COMMAND "${CMAKE_COMMAND}" -E env ${AGY_CONVERT_ENV}
          python3 -m symbols.make_libagy "${AGY_RAW}" vendor/libagy.so symbols/symbols.json
  WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
  DEPENDS "${AGY_RAW}"
          "${CMAKE_SOURCE_DIR}/symbols/make_libagy.py"
          "${CMAKE_SOURCE_DIR}/symbols/build_symbols.py"
          "${CMAKE_SOURCE_DIR}/symbols/agyelf.py"
          "${CMAKE_SOURCE_DIR}/symbols/__init__.py"
          "${CMAKE_SOURCE_DIR}/src/procdef.h"
  COMMENT "convert raw agy + capture metadata before patching"
  VERBATIM)
add_custom_target(agy_library DEPENDS "${CMAKE_SOURCE_DIR}/vendor/libagy.so")

# ---- Linux UAPI headers: glibc <limits.h> needs <linux/limits.h>. Reference the
# conda kernel-headers sysroot in place via -idirafter; empty on a host that has /usr/include/linux.
set(UAPI_INC "")
if(NOT EXISTS "/usr/include/linux/limits.h")
  foreach(_p "$ENV{CONDA_PREFIX}" "$ENV{PREFIX}" "$ENV{BUILD_PREFIX}")
    if(_p AND EXISTS "${_p}/x86_64-conda-linux-gnu/sysroot/usr/include/linux/limits.h")
      set(UAPI_INC "${_p}/x86_64-conda-linux-gnu/sysroot/usr/include")
      break()
    endif()
  endforeach()
  if(NOT UAPI_INC)
    message(FATAL_ERROR "no linux/limits.h — install kernel-headers_linux-64 (a pixi dep) so "
                        "$CONDA_PREFIX has a sysroot (CONDA_PREFIX=${CONDA_PREFIX})")
  endif()
endif()
message(STATUS "[build] UAPI headers: ${UAPI_INC} (empty = /usr/include)")

# NOTE: no python3-config probe here. No shim TU includes a Python header — CPython is embedded by
# the SHARED bridge (wirecap/native), which does its own probe and carries the embed link flags.
# ---- generate the BuildID, Go frames and stub ids from symbols.json + src/procdef.h -----------
add_custom_command(
  OUTPUT "${CMAKE_SOURCE_DIR}/src/symbols_gen.h"
  COMMAND python3 -m symbols.gen_sources
                  "${CMAKE_SOURCE_DIR}/symbols/symbols.json" "${CMAKE_SOURCE_DIR}/src/symbols_gen.h"
  WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
  DEPENDS "${CMAKE_SOURCE_DIR}/symbols/symbols.json"
          "${CMAKE_SOURCE_DIR}/symbols/__init__.py"
          "${CMAKE_SOURCE_DIR}/symbols/gen_sources.py"
          "${CMAKE_SOURCE_DIR}/symbols/agyelf.py"
          "${CMAKE_SOURCE_DIR}/src/procdef.h"
  COMMENT "gen src/symbols_gen.h from symbols.json + src/procdef.h"
  VERBATIM)
add_custom_target(gen_symbols DEPENDS "${CMAKE_SOURCE_DIR}/src/symbols_gen.h")

# ---- the shared native bridge (embedded CPython worker) — the wire_* C ABI the shim calls.
# Extracted to wirecap/native so the codex build links the same bridge; built as a static lib
# whose PUBLIC usage requirements (wirecap.h include + libpython embed + boost + rpath) flow into
# the shim below. ------------------------------------------------------------------------------
add_subdirectory("${CMAKE_SOURCE_DIR}/../wirecap/native" "${CMAKE_BINARY_DIR}/wirecap_bridge")

# ---- the instrumented host executable: vendor/agy (links vendor/libagy.so first) --------------
# src/goframe.cpp derives active rows from BOOKS and instantiates naked templates;
# symbols_gen.h supplies the expected BuildID, original Go frame sizes and literal stub ids.
# GCC 15 constexpr asm emits the stubs; C++ owns the constexpr descriptor table and span API.
add_executable(agy
  src/antigravity.cpp src/gomod.cpp src/gocall.cpp src/goframe.cpp)
add_dependencies(agy gen_symbols agy_library)
set_target_properties(agy PROPERTIES
  OUTPUT_NAME "agy"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/vendor")

target_compile_features(agy PRIVATE cxx_std_23)
# All the shim's TUs are C++23. -Wno-deprecated-declarations: one std::auto_ptr warning from
# Boost.Python (via the bridge's propagated includes). -Wno-missing-field-initializers: the shim uses
# partial designated initializers pervasively (C's -Wextra exempts them, C++'s doesn't).
target_compile_options(agy PRIVATE
  -O2 -g -fPIC -Wall -Wextra -Wno-deprecated-declarations -Wno-missing-field-initializers
  -fvisibility=hidden -ffunction-sections -fdata-sections)
if(UAPI_INC)
  target_compile_options(agy PRIVATE -idirafter "${UAPI_INC}")
endif()
# GCC LTO across the host TUs and the bridge (TASKSOLVER_LTO is defined by wirecap/native above).
# The handler table named only in asm is marked used and externally_visible.
if(TASKSOLVER_LTO)
  target_compile_options(agy PRIVATE -flto=auto)
  target_link_options(agy PRIVATE -flto=auto)
  # ...except goframe.cpp: preserve the templates' asm-defined export/boundary labels and
  # their external references. Metadata uses linked addresses. See test_agy_stubs.py.
  set_source_files_properties(src/goframe.cpp PROPERTIES COMPILE_OPTIONS "-fno-lto")
endif()
target_include_directories(agy PRIVATE "${CONDA_PREFIX}/include")

# Link order is critical: vendor/libagy.so MUST be first so its PT_TLS segment receives the first
# static TLS slot [%fs:-0x4a0 .. %fs:0) from ld-linux-x86-64.so.2 (the host executable itself
# defines no thread-local variables). Followed by wirecap_bridge and system libs.
target_link_libraries(agy PRIVATE
  "${CMAKE_SOURCE_DIR}/vendor/libagy.so"
  wirecap_bridge                       # the shared bridge (PUBLIC-propagates libpython embed + boost + rpath)
  -L${CONDA_PREFIX}/lib rt resolv dl m -pthread)
target_link_options(agy PRIVATE
  -Wl,--exclude-libs,ALL -Wl,--gc-sections -Wl,--enable-new-dtags
  "-Wl,-rpath,\$ORIGIN:\$ORIGIN/../../../..:${CONDA_PREFIX}/lib")

# ---- maintenance targets ----------------------------------------------------------------------
add_custom_target(symbols)
add_dependencies(symbols agy_library)
add_custom_target(agy-update
  COMMAND python3 -m symbols.make_libagy --update
  WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
  COMMENT "query the LATEST release manifest + print new pin values for this CMakeLists"
  VERBATIM USES_TERMINAL)

# ---- clangd: expose the compile db at the source root (dangling until the first configure writes
# it, then resolves). gitignored. -----------------------------------------------------------------
file(CREATE_LINK "${CMAKE_BINARY_DIR}/compile_commands.json"
     "${CMAKE_SOURCE_DIR}/compile_commands.json" SYMBOLIC)
