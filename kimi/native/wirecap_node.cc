// wirecap_node — N-API host for the wirecap bridge inside kimi-code (Node).
//
// The JS side (the vendored tree's `packages/agent-core-v2/src/wiretap/wiretap.ts`
// patch) loads this addon via createRequire($WIRE_NODE_ADDON) and calls
// start() once at process start; the three emit entry points forward
// UTF-8 JSON payloads to the embedded-CPython bridge (libwirecap_bridge.a)
// exactly like codex's `codex-rs/wirecap/src/lib.rs` Rust shim:
//
//   emitRequest(bytes) -> new monotonic turn id;  kind "kimi_request"
//   emitEvent(bytes)   -> current turn id;        kind "kimi_event"
//   emitWire(bytes)    -> current turn id;        kind "kimi_wire"
//
// Constraints inherited from the bridge (wirecap/native/wirecap.h):
//   - kind strings must be immortal C literals (job.kind is stored unowned);
//   - ASYNC only from JS — wire_emit_async copies the payload on this thread
//     before queueing, so a Uint8Array view pointer is safe to pass;
//   - wire_start() blocks the calling thread through CPython init + the
//     $WIRE_MODULE import; one start/shutdown pair per process, no restart.
//
// Built against N-API version 8 so one .node works across Node 22/24/25.

#define NAPI_VERSION 8
#include <node_api.h>

#include <dlfcn.h>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <mutex>

#include "wirecap.h"

#ifndef WIRE_PYTHON_SONAME
#define WIRE_PYTHON_SONAME "libpython3.so"
#endif

namespace {

std::atomic<uint64_t> g_turn{0};
std::once_flag g_start_once;
int g_start_rc = -1;
std::atomic<bool> g_started{false};

const char KIND_REQUEST[] = "kimi_request";
const char KIND_EVENT[] = "kimi_event";
const char KIND_WIRE[] = "kimi_wire";

bool payload_bytes(napi_env env, napi_value value, const uint8_t **data, size_t *len) {
  bool is_typedarray = false;
  if (napi_is_typedarray(env, value, &is_typedarray) != napi_ok || !is_typedarray) return false;
  napi_typedarray_type type;
  size_t length = 0;
  void *raw = nullptr;
  if (napi_get_typedarray_info(env, value, &type, &length, &raw, nullptr, nullptr) != napi_ok)
    return false;
  if (type != napi_uint8_array) return false;
  *data = static_cast<const uint8_t *>(raw);
  *len = length;
  return true;
}

// Promote the embedded libpython to the GLOBAL symbol namespace. node dlopen()s this addon
// RTLD_LOCAL, so libpython — loaded as the addon's (transitive) dependency — lands in a local
// scope. The stdlib C extensions the interpreter dlopens later (_hashlib, _sha2, _json, …) then
// fail to resolve Python C-API symbols (PyModule_AddType, PyModuleDef_Init) against it. A codex
// binary never hits this: it NEEDs libpython at process start, which is global. RTLD_NOLOAD finds
// the already-resident libpython and RTLD_GLOBAL promotes its symbols in place. Must run BEFORE
// Py_InitializeEx (inside wire_start), or the first extension imports still fail.
static void promote_libpython_global() {
  if (dlopen(WIRE_PYTHON_SONAME, RTLD_NOW | RTLD_GLOBAL | RTLD_NOLOAD) == nullptr)
    std::fprintf(stderr, "[wirecap_node] warning: could not promote %s to RTLD_GLOBAL: %s\n",
                 WIRE_PYTHON_SONAME, dlerror());
}

napi_value js_start(napi_env env, napi_callback_info) {
  std::call_once(g_start_once, [] {
    const char *enable = std::getenv("WIRE_ENABLE");
    if (enable == nullptr || enable[0] == '\0') return;  // g_start_rc stays -1
    promote_libpython_global();
    g_start_rc = wire_start();
    g_started.store(g_start_rc == 0, std::memory_order_release);
  });
  napi_value out;
  napi_create_int32(env, g_start_rc, &out);
  return out;
}

napi_value js_ready(napi_env env, napi_callback_info) {
  napi_value out;
  napi_create_int32(env, wire_ready(), &out);
  return out;
}

napi_value emit_kind(napi_env env, napi_callback_info info, const char *kind, uint64_t stream_id) {
  size_t argc = 1;
  napi_value argv[1];
  napi_get_cb_info(env, info, &argc, argv, nullptr, nullptr);
  const uint8_t *data = nullptr;
  size_t len = 0;
  if (argc >= 1 && payload_bytes(env, argv[0], &data, &len) && wire_ready())
    wire_emit_async(kind, stream_id, data, len);
  return nullptr;
}

napi_value js_emit_request(napi_env env, napi_callback_info info) {
  // The counter advances even when the emit is dropped (bridge not ready),
  // matching codex's emit_request: turn ids stay monotonic per request.
  const uint64_t id = g_turn.fetch_add(1, std::memory_order_relaxed) + 1;
  emit_kind(env, info, KIND_REQUEST, id);
  napi_value out;
  napi_create_double(env, static_cast<double>(id), &out);
  return out;
}

napi_value js_emit_event(napi_env env, napi_callback_info info) {
  return emit_kind(env, info, KIND_EVENT, g_turn.load(std::memory_order_relaxed));
}

napi_value js_emit_wire(napi_env env, napi_callback_info info) {
  return emit_kind(env, info, KIND_WIRE, g_turn.load(std::memory_order_relaxed));
}

napi_value js_shutdown(napi_env, napi_callback_info) {
  if (g_started.exchange(false)) wire_shutdown();  // drains pending jobs, joins the worker
  return nullptr;
}

napi_value init(napi_env env, napi_value exports) {
  const napi_property_descriptor props[] = {
      {"start", nullptr, js_start, nullptr, nullptr, nullptr, napi_default, nullptr},
      {"ready", nullptr, js_ready, nullptr, nullptr, nullptr, napi_default, nullptr},
      {"emitRequest", nullptr, js_emit_request, nullptr, nullptr, nullptr, napi_default, nullptr},
      {"emitEvent", nullptr, js_emit_event, nullptr, nullptr, nullptr, napi_default, nullptr},
      {"emitWire", nullptr, js_emit_wire, nullptr, nullptr, nullptr, napi_default, nullptr},
      {"shutdown", nullptr, js_shutdown, nullptr, nullptr, nullptr, napi_default, nullptr},
  };
  napi_define_properties(env, exports, sizeof(props) / sizeof(props[0]), props);
  return exports;
}

}  // namespace

NAPI_MODULE_INIT() { return init(env, exports); }
