# antigravity — build `vendor/libagy.so` and the linked host executable (`vendor/agy`).
#
# Configure + build (both done by setup.py's build_py hook during `pixi install`, and by the
# `build-shim` pixi task):
#   cmake -S antigravity -B antigravity/build -G Ninja
#   cmake --build antigravity/build
# Maintenance targets (see the pixi tasks shim-symbols / shim-agy-update):
#   cmake --build antigravity/build --target symbols     # re-resolve symbols.json from vendor/libagy.so
#   cmake --build antigravity/build --target agy-update   # fetch the latest agy + print new pins
#
# Configure-time "setup": fetch the pinned agy (sha512-verified) and convert it into
# vendor/libagy.so (idempotent; guarded by existence), then the build compiles the three C++23 TUs.
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
# sha512-verified release so the addresses in symbols.json stay valid. AGY_BIN is an escape hatch:
# point it at a local agy build to vendor instead of downloading (must match symbols.json's build-id).
#
# Pinned from the GitHub releases (github.com/google-antigravity/antigravity-cli), NOT the GCS
# auto-updater: the updater only ever serves "latest" and the bucket denies listing, so it cannot
# fetch a specific older version — while the releases expose every version at a stable URL. The
# assets are the same official artifacts: 1.0.16's asset sha512 is byte-identical to the GCS
# tarball we ran for weeks, and the tarball member is still `antigravity` (extracted below).
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
# firing. Re-fetch the pin (rm vendor/agy && pixi run build-shim) or `pixi run shim-symbols` to
# re-resolve against whatever landed; `chmod a-w vendor/agy` blunts the in-place overwrite.
set(AGY_VERSION "1.4.11")
set(AGY_URL "https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.11-5180209989812224/linux-x64/cli_linux_x64.tar.gz")
set(AGY_SHA512 "2935601efa745ed6e314d32b415c71f44d9d455f0e6a2583372584be5a9074c28154cf81d5eec65b4416579453a3b3b05d3abe4d4f176754d5ef93a84af6cf28")
set(AGY_BIN "" CACHE STRING "optional: local agy binary to vendor instead of downloading")
# WSL1's 4.4 kernel rejects tcmalloc's MAP_FIXED_NOREPLACE mmap flag; a freshly-fetched agy must be
# byte-patched to run there (address- & build-id-preserving). Auto-detect; override with -DAGY_WSL1=.
execute_process(COMMAND uname -r OUTPUT_VARIABLE _uname_r OUTPUT_STRIP_TRAILING_WHITESPACE)
if(NOT DEFINED AGY_WSL1)
  if(_uname_r MATCHES "Microsoft")
    set(AGY_WSL1 1)
  else()
    set(AGY_WSL1 0)
  endif()
endif()

file(MAKE_DIRECTORY "${CMAKE_SOURCE_DIR}/vendor")

# ---- vendor libagy.so (pinned agy converted to shared library, sha512-verified) ---------------
if(EXISTS "${CMAKE_SOURCE_DIR}/vendor/libagy.so")
  message(STATUS "[setup] libagy.so present")
elseif(AGY_BIN AND EXISTS "${AGY_BIN}")
  file(COPY_FILE "${AGY_BIN}" "${CMAKE_BINARY_DIR}/antigravity.raw")
  message(STATUS "[setup] vendoring libagy.so from ${AGY_BIN}")
  if(AGY_WSL1)
    execute_process(COMMAND python3 "${CMAKE_SOURCE_DIR}/symbols/patch_agy_wsl1.py" "${CMAKE_BINARY_DIR}/antigravity.raw"
                    RESULT_VARIABLE _wsl1_rc)
    if(NOT _wsl1_rc EQUAL 0)
      message(FATAL_ERROR "patch_agy_wsl1.py failed")
    endif()
  endif()
  execute_process(
    COMMAND python3 "${CMAKE_SOURCE_DIR}/symbols/make_libagy.py"
            "${CMAKE_BINARY_DIR}/antigravity.raw" "${CMAKE_SOURCE_DIR}/vendor/libagy.so"
    RESULT_VARIABLE _mk_rc)
  file(REMOVE "${CMAKE_BINARY_DIR}/antigravity.raw" "${CMAKE_BINARY_DIR}/antigravity.raw.orig")
  if(NOT _mk_rc EQUAL 0)
    message(FATAL_ERROR "make_libagy.py failed")
  endif()
elseif(EXISTS "${CMAKE_SOURCE_DIR}/vendor/agy")
  file(SIZE "${CMAKE_SOURCE_DIR}/vendor/agy" _existing_agy_sz)
  if(_existing_agy_sz GREATER 50000000)
    message(STATUS "[setup] converting existing raw vendor/agy -> vendor/libagy.so")
    execute_process(
      COMMAND python3 "${CMAKE_SOURCE_DIR}/symbols/make_libagy.py"
              "${CMAKE_SOURCE_DIR}/vendor/agy" "${CMAKE_SOURCE_DIR}/vendor/libagy.so"
      RESULT_VARIABLE _mk_rc)
    if(NOT _mk_rc EQUAL 0)
      message(FATAL_ERROR "make_libagy.py failed")
    endif()
    file(REMOVE "${CMAKE_SOURCE_DIR}/vendor/agy")
  endif()
endif()

if(NOT EXISTS "${CMAKE_SOURCE_DIR}/vendor/libagy.so")
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
    message(FATAL_ERROR "agy extract failed")
  endif()
  if(AGY_WSL1)
    execute_process(COMMAND python3 "${CMAKE_SOURCE_DIR}/symbols/patch_agy_wsl1.py" "${CMAKE_BINARY_DIR}/antigravity"
                    RESULT_VARIABLE _wsl1_rc)
    if(NOT _wsl1_rc EQUAL 0)
      message(FATAL_ERROR "patch_agy_wsl1.py failed")
    endif()
  endif()
  execute_process(
    COMMAND python3 "${CMAKE_SOURCE_DIR}/symbols/make_libagy.py"
            "${CMAKE_BINARY_DIR}/antigravity" "${CMAKE_SOURCE_DIR}/vendor/libagy.so"
    RESULT_VARIABLE _mk_rc)
  file(REMOVE "${CMAKE_BINARY_DIR}/antigravity" "${CMAKE_BINARY_DIR}/antigravity.orig")
  if(NOT _mk_rc EQUAL 0)
    message(FATAL_ERROR "make_libagy.py failed")
  endif()
  message(STATUS "[setup] vendored libagy.so ${AGY_VERSION}")
endif()

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
# ---- generate symbols_gen.h from the committed symbols.json + src/procdef.h -------------------
add_custom_command(
  OUTPUT "${CMAKE_SOURCE_DIR}/src/symbols_gen.h"
  COMMAND python3 "${CMAKE_SOURCE_DIR}/symbols/gen_symbols_header.py"
                  "${CMAKE_SOURCE_DIR}/symbols/symbols.json" "${CMAKE_SOURCE_DIR}/src/symbols_gen.h"
  DEPENDS "${CMAKE_SOURCE_DIR}/symbols/symbols.json"
          "${CMAKE_SOURCE_DIR}/symbols/gen_symbols_header.py"
          "${CMAKE_SOURCE_DIR}/symbols/gen_hooks_py.py"
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
add_executable(agy
  src/antigravity.cpp src/gomod.cpp src/gocall.cpp)
add_dependencies(agy gen_symbols)
set_target_properties(agy PROPERTIES
  OUTPUT_NAME "agy"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/vendor"
  ENABLE_EXPORTS ON)

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
# The C symbols the toplevel trampoline asm calls are marked AGY_ASM_REFERENCED (gocall.h).
if(TASKSOLVER_LTO)
  target_compile_options(agy PRIVATE -flto=auto)
  target_link_options(agy PRIVATE -flto=auto)
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
add_custom_target(symbols
  # Run from the source dir with RELATIVE paths so the recorded agy_path stays "vendor/libagy.so".
  COMMAND python3 symbols/build_symbols.py vendor/libagy.so symbols/symbols.json
  WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
  COMMENT "re-resolve symbols.json from vendor/libagy.so (after an agy update)"
  VERBATIM USES_TERMINAL)
add_custom_target(agy-update
  COMMAND bash "${CMAKE_SOURCE_DIR}/symbols/agy_update.sh"
  COMMENT "fetch the LATEST agy + print new pin values for this CMakeLists"
  VERBATIM USES_TERMINAL)

# ---- clangd: expose the compile db at the source root (dangling until the first configure writes
# it, then resolves). gitignored. -----------------------------------------------------------------
file(CREATE_LINK "${CMAKE_BINARY_DIR}/compile_commands.json"
     "${CMAKE_SOURCE_DIR}/compile_commands.json" SYMBOLIC)
