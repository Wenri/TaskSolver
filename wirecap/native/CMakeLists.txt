# wirecap_bridge — the shared native bridge (embedded CPython worker) as a static library,
# linked into the agy host executable and the kimi N-API addon (antigravity/CMakeLists.txt and
# kimi/native/CMakeLists.txt add_subdirectory this) and into the patched codex binary (its build.rs
# links libwirecap_bridge.a from this project's standalone build, `cmake -S wirecap/native -B
# wirecap/native/build`, which setup.py runs before cargo). PUBLIC usage requirements (the wirecap.h
# include dir + the libpython embed + boost link + the conda rpath) propagate to whatever links it,
# so a consumer just does target_link_libraries(... wirecap_bridge).
#
# Like the shim, this MUST be built with the conda toolchain (CXX=x86_64-conda-linux-gnu-c++) so it
# links the conda glibc + libpython3.15 — configure inside the pixi env.
cmake_minimum_required(VERSION 3.19)
project(wirecap_bridge CXX)

set(CONDA_PREFIX "$ENV{CONDA_PREFIX}")
if(NOT CONDA_PREFIX)
  message(FATAL_ERROR "CONDA_PREFIX is not set — configure inside the pixi env.")
endif()

# Embed conda's libpython (same python3-config the shim uses).
find_program(PY3CONFIG NAMES python3-config HINTS "${CONDA_PREFIX}/bin" REQUIRED)
execute_process(COMMAND "${PY3CONFIG}" --includes OUTPUT_VARIABLE _py_inc OUTPUT_STRIP_TRAILING_WHITESPACE)
execute_process(COMMAND "${PY3CONFIG}" --ldflags --embed OUTPUT_VARIABLE _py_ld OUTPUT_STRIP_TRAILING_WHITESPACE)
separate_arguments(WIRE_PY_INCLUDES UNIX_COMMAND "${_py_inc}")
separate_arguments(WIRE_PY_LDFLAGS  UNIX_COMMAND "${_py_ld}")
# Boost.Python's lib name carries the python version (libboost_python313, …314); derive the tag
# from the same env's python3 so the link matches whatever libpython we just embedded.
execute_process(COMMAND "${CONDA_PREFIX}/bin/python3" -c "import sys;print('%d%d' % sys.version_info[:2])"
                OUTPUT_VARIABLE PY_TAG OUTPUT_STRIP_TRAILING_WHITESPACE)
if(NOT PY_TAG)
  message(FATAL_ERROR "could not derive the python version tag from ${CONDA_PREFIX}/bin/python3")
endif()

# Link-time optimization for all of our C++: this bridge, the agy host and the kimi addon (the
# consumers check TASKSOLVER_LTO too). GCC LTO for now; switching these targets to clang would let
# the bridge also LTO with the Rust codex binary. The bridge ships *fat* LTO objects (GIMPLE plus
# regular machine code): agy and the kimi addon link with -flto and optimize across it, while
# codex's cargo/rustc link cannot read GCC bytecode and uses the machine code. gcc-ar indexes the
# archive through the LTO plugin.
option(TASKSOLVER_LTO "Build the native C++ (bridge, agy host, kimi addon) with link-time optimization" ON)
if(TASKSOLVER_LTO AND CMAKE_CXX_COMPILER_AR AND CMAKE_CXX_COMPILER_RANLIB)
  set(CMAKE_CXX_ARCHIVE_CREATE "\"${CMAKE_CXX_COMPILER_AR}\" qc <TARGET> <LINK_FLAGS> <OBJECTS>")
  set(CMAKE_CXX_ARCHIVE_APPEND "\"${CMAKE_CXX_COMPILER_AR}\" q <TARGET> <LINK_FLAGS> <OBJECTS>")
  set(CMAKE_CXX_ARCHIVE_FINISH "\"${CMAKE_CXX_COMPILER_RANLIB}\" <TARGET>")
endif()

add_library(wirecap_bridge STATIC pybridge.cpp)
target_compile_features(wirecap_bridge PRIVATE cxx_std_23)
# -Wno-deprecated-declarations: one std::auto_ptr warning from Boost.Python.
# -Wno-missing-field-initializers: partial designated initializers (C's -Wextra exempts them).
target_compile_options(wirecap_bridge PRIVATE
  -O2 -g -fPIC -Wall -Wextra -Wno-deprecated-declarations -Wno-missing-field-initializers
  -fvisibility=hidden ${WIRE_PY_INCLUDES})
if(TASKSOLVER_LTO)
  target_compile_options(wirecap_bridge PRIVATE -flto=auto -ffat-lto-objects)
  target_link_options(wirecap_bridge INTERFACE -flto=auto)   # CMake consumers LTO across the bridge
endif()
target_include_directories(wirecap_bridge PRIVATE "${CONDA_PREFIX}/include")
target_include_directories(wirecap_bridge PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}")   # wirecap.h

# PUBLIC so consumers inherit the libpython embed + boost + rpath. boost_python${PY_TAG} keeps
# the python-version-specific Boost.Python in lockstep with the embedded libpython.
target_link_libraries(wirecap_bridge PUBLIC
  ${WIRE_PY_LDFLAGS} -L${CONDA_PREFIX}/lib boost_thread boost_python${PY_TAG} -pthread)
target_link_options(wirecap_bridge PUBLIC -Wl,-rpath,${CONDA_PREFIX}/lib)
