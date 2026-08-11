# wirecap_node.node — the N-API addon that hosts the wirecap bridge (embedded
# CPython) inside the vendored kimi-code CLI.  Sibling of the codex build,
# where cargo links the same libwirecap_bridge.a into the Rust binary; here
# the bridge is linked into a Node addon instead and the (patched) JS loads it
# via $WIRE_NODE_ADDON.
#
#   cmake -S kimi/native -B kimi/native/build -G Ninja && cmake --build kimi/native/build
#
# Needs $CONDA_PREFIX (python3-config, boost, and the conda nodejs package's
# N-API headers at $CONDA_PREFIX/include/node).  napi_* symbols stay undefined
# in the .so and resolve from the host node binary at dlopen — that is the
# standard N-API link model on Linux, no libnode to link.
cmake_minimum_required(VERSION 3.19)
project(wirecap_node CXX)

if(NOT DEFINED ENV{CONDA_PREFIX})
  message(FATAL_ERROR "CONDA_PREFIX is not set — build inside the pixi/conda env "
                      "(the bridge derives python/boost flags from it)")
endif()
set(CONDA_PREFIX $ENV{CONDA_PREFIX})

# The shared bridge (embeds CPython; PUBLIC usage requirements carry the
# python/boost link flags and the $CONDA_PREFIX/lib rpath).
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../../wirecap/native
                 ${CMAKE_BINARY_DIR}/wirecap_bridge)

find_path(NODE_API_INC node_api.h HINTS "${CONDA_PREFIX}/include/node" REQUIRED)

# The libpython the bridge embeds — promoted to RTLD_GLOBAL at start() so the interpreter's
# stdlib C extensions resolve the Python C-API. Build the shared name from LDVERSION
# (libpython3.13.so, a symlink to the versioned .so.1.0 the addon already NEEDs): this conda
# build sets INSTSONAME/LDLIBRARY to the static archive, so query neither. The addon dlopen()s
# it RTLD_NOLOAD, which matches the already-resident object by resolved path.
execute_process(
  COMMAND "${CONDA_PREFIX}/bin/python3" -c
          "import sysconfig; print('libpython' + sysconfig.get_config_var('LDVERSION') + '.so')"
  OUTPUT_VARIABLE WIRE_PYTHON_SONAME OUTPUT_STRIP_TRAILING_WHITESPACE)

add_library(wirecap_node SHARED wirecap_node.cc)
set_target_properties(wirecap_node PROPERTIES PREFIX "" SUFFIX ".node")
target_compile_features(wirecap_node PRIVATE cxx_std_23)
target_compile_options(wirecap_node PRIVATE -O2 -g -fPIC -Wall -Wextra)
target_compile_definitions(wirecap_node PRIVATE
  "WIRE_PYTHON_SONAME=\"${WIRE_PYTHON_SONAME}\"")
target_include_directories(wirecap_node PRIVATE ${NODE_API_INC})
target_link_libraries(wirecap_node PRIVATE wirecap_bridge)
# Second rpath entry makes the wheel layout self-resolving:
# <prefix>/lib/pythonX.Y/site-packages/pykimi/vendor/wirecap_node.node -> <prefix>/lib.
target_link_options(wirecap_node PRIVATE
  "-Wl,--enable-new-dtags,-rpath,$ORIGIN/../../../..")
