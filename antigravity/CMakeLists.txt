# antigravity — build `vendor/libagy.so` and the linked host executable (`vendor/agy`).
#
# Configure + build (both done by setup.py's build_py step during `pixi install`, and by the
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

# Configure inside pixi so CMake selects the conda compiler. Nix GCC embeds glibc
# rpaths whose symbol requirements can prevent the host from starting.
if(NOT CMAKE_CXX_COMPILER MATCHES "conda")
  message(WARNING "CMAKE_CXX_COMPILER='${CMAKE_CXX_COMPILER}' is not a conda wrapper — on a Nix host "
                  "this embeds /nix glibc rpaths that can break startup. Configure inside the pixi env.")
endif()

set(CONDA_PREFIX "$ENV{CONDA_PREFIX}")
if(NOT CONDA_PREFIX)
  message(FATAL_ERROR "CONDA_PREFIX is not set — configure inside the pixi env (pixi run build-shim).")
endif()

# ---- pinned agy release: vendor.json (then `pixi run shim-symbols`) ----------------------------
# archive_sha512 verifies the download; sha512 and size verify the extracted binary, which is also
# what `python -m tasksolver.reconstruct --agy` checks a locally installed agy against. Only
# agy-update consults the release manifest. AGY_BIN can select another raw binary. Never convert
# vendor/agy: it is the linked host. If agy's self-updater replaces that host, remove vendor/agy
# and rebuild it.
file(READ "${CMAKE_SOURCE_DIR}/vendor.json" _agy_pin)
string(JSON AGY_VERSION GET "${_agy_pin}" cli version)
string(JSON AGY_URL GET "${_agy_pin}" cli url)
string(JSON AGY_SHA512 GET "${_agy_pin}" cli archive_sha512)
string(JSON AGY_RAW_SHA512 GET "${_agy_pin}" cli sha512)
string(JSON AGY_RAW_SIZE GET "${_agy_pin}" cli size)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_SOURCE_DIR}/vendor.json")
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
  file(SIZE "${CMAKE_BINARY_DIR}/antigravity" _agy_raw_size)
  file(SHA512 "${CMAKE_BINARY_DIR}/antigravity" _agy_raw_sha512)
  if(NOT _agy_raw_size EQUAL AGY_RAW_SIZE OR NOT _agy_raw_sha512 STREQUAL AGY_RAW_SHA512)
    file(REMOVE "${CMAKE_BINARY_DIR}/antigravity")
    message(FATAL_ERROR "the extracted agy does not match vendor.json (size ${_agy_raw_size}, "
                        "sha512 ${_agy_raw_sha512}); record both with a new archive pin")
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
          "${CMAKE_SOURCE_DIR}/symbols/x86.py"
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

# wirecap/native discovers CPython and supplies its include/link flags.
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
          "${CMAKE_SOURCE_DIR}/symbols/x86.py"
          "${CMAKE_SOURCE_DIR}/src/procdef.h"
  COMMENT "gen src/symbols_gen.h from symbols.json + src/procdef.h"
  VERBATIM)
add_custom_target(gen_symbols DEPENDS "${CMAKE_SOURCE_DIR}/src/symbols_gen.h")

# ---- shared static bridge: CPython worker and wire_* C ABI ---------------------------------
add_subdirectory("${CMAKE_SOURCE_DIR}/../wirecap/native" "${CMAKE_BINARY_DIR}/wirecap_bridge")

# ---- the linked host executable: vendor/agy (links vendor/libagy.so first) --------------
# src/goframe.cpp derives active rows from BOOKS and instantiates naked templates;
# symbols_gen.h supplies the expected BuildID, original Go frame sizes and literal stub ids.
# GCC 15 constexpr asm emits the stubs; C++ owns the constexpr descriptor table and span API.
add_executable(agy
  src/antigravity.cpp src/gomod.cpp src/gocall.cpp src/goframe.cpp)
add_dependencies(agy gen_symbols agy_library)
# The link options below give the whole RUNPATH. CMake's build-tree entry would add this
# checkout's vendor/ (libagy.so is linked by path), which $ORIGIN already covers; a packaged agy
# would keep the build directory there, a path the Nix build rejects.
set_target_properties(agy PROPERTIES
  OUTPUT_NAME "agy"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/vendor"
  SKIP_BUILD_RPATH ON)

target_compile_features(agy PRIVATE cxx_std_23)
# Suppress Boost.Python's std::auto_ptr warning and allow partial designated initializers.
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

# ---- clangd: expose the generated compile database at the source root ----------------------
file(CREATE_LINK "${CMAKE_BINARY_DIR}/compile_commands.json"
     "${CMAKE_SOURCE_DIR}/compile_commands.json" SYMBOLIC)
