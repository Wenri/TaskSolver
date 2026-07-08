# wirecap_bridge — the shared native bridge (embedded CPython worker) as a static library,
# linked by both the antigravity LD_PRELOAD shim (antigravity/CMakeLists.txt add_subdirectory's
# this) and the patched codex build (its build.rs links libwirecap_bridge.a). PUBLIC usage
# requirements (the wirecap.h include dir + the libpython embed + boost link + the conda rpath)
# propagate to whatever links it, so a consumer just does target_link_libraries(... wirecap_bridge).
#
# Like the shim, this MUST be built with the conda toolchain (CXX=x86_64-conda-linux-gnu-c++) so it
# links the conda glibc + libpython3.13 — configure inside the pixi env.
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

add_library(wirecap_bridge STATIC pybridge.cpp)
target_compile_features(wirecap_bridge PRIVATE cxx_std_23)
# -Wno-deprecated-declarations: one std::auto_ptr warning from Boost.Python.
# -Wno-missing-field-initializers: partial designated initializers (C's -Wextra exempts them).
target_compile_options(wirecap_bridge PRIVATE
  -O2 -g -fPIC -Wall -Wextra -Wno-deprecated-declarations -Wno-missing-field-initializers
  -fvisibility=hidden ${WIRE_PY_INCLUDES})
target_include_directories(wirecap_bridge PRIVATE "${CONDA_PREFIX}/include")
target_include_directories(wirecap_bridge PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}")   # wirecap.h

# PUBLIC so consumers inherit the libpython embed + boost + rpath. boost_python${PY_TAG} keeps
# the python-version-specific Boost.Python in lockstep with the embedded libpython.
target_link_libraries(wirecap_bridge PUBLIC
  ${WIRE_PY_LDFLAGS} -L${CONDA_PREFIX}/lib boost_thread boost_python${PY_TAG} -pthread)
target_link_options(wirecap_bridge PUBLIC -Wl,-rpath,${CONDA_PREFIX}/lib)
