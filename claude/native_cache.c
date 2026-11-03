#define _GNU_SOURCE
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The private Bun's C++ entry points are stripped. claude/unbundle.py writes bun_natives.h
 * from `bun_graph --natives`, which locates them by their instructions in the pinned build:
 * WIRE_BUN_* addresses and the WIRE_BUN_*_CODE bytes libwire.node re-checks before calling. */
#include "bun_natives.h"

struct bun_string {
    uint8_t tag;
    uint8_t padding[7];
    uintptr_t bytes;
    size_t length;
};
_Static_assert(sizeof(struct bun_string) == 24, "private BunString ABI");
_Static_assert(offsetof(struct bun_string, bytes) == 8, "private BunString ABI");

#ifdef TASKSOLVER_BUN_UTF8_SHIM
/* The selected sidecar-loader call uses the C aggregate-return ABI. Keep the
 * conversion inside Bun so source code, including raw templates, is unchanged. */
__attribute__((used, no_stack_protector, section(".text.wire_bun_clone_utf8")))
struct bun_string wire_bun_clone_utf8(const unsigned char *bytes, size_t length)
{
    if (!length)
        return (struct bun_string){4, {0}, 0, 0};
    const struct bun_string input = {2, {0}, (uintptr_t)bytes | (UINT64_C(1) << 61), length};
    void *impl;
    typedef void (*convert_fn)(void **, const struct bun_string *);
    ((convert_fn)(uintptr_t)WIRE_BUN_TO_WTF)(&impl, &input);
    return (struct bun_string){1, {0}, (uintptr_t)impl, 0};
}
#else
#include <link.h>
#include <stdlib.h>
#include <string.h>
#define NAPI_VERSION 8
#include <node_api.h>

/* The private Bun is ET_EXEC, so its addresses are absolute. ZigSourceProvider.cpp owns
 * generation; Bun releases a CachedBytecode inline (its destructor, then mi_free), and
 * wire_bun_emit_module repeats that release for the reference it receives.
 */
struct target {
    uintptr_t address;
    const unsigned char *code;
    size_t length;
    bool executable;
};

static const unsigned char emit_code[] = WIRE_BUN_EMIT_CODE;
static const unsigned char destroy_code[] = WIRE_BUN_DESTROY_CODE;
static const unsigned char free_code[] = WIRE_BUN_FREE_CODE;

static int mapped_targets(struct dl_phdr_info *info, size_t size, void *opaque)
{
    (void)size;
    struct target *targets = opaque;
    /* Only the main, non-PIE executable supplies these private addresses. */
    if (info->dlpi_addr != 0 || (info->dlpi_name && *info->dlpi_name))
        return 0;
    for (size_t i = 0; i < info->dlpi_phnum; ++i) {
        const ElfW(Phdr) *p = &info->dlpi_phdr[i];
        if (p->p_type != PT_LOAD || !(p->p_flags & PF_X))
            continue;
        for (size_t j = 0; j < 3; ++j) {
            struct target *t = &targets[j];
            if (t->address >= p->p_vaddr && t->address - p->p_vaddr <= p->p_memsz
                && t->length <= p->p_memsz - (t->address - p->p_vaddr))
                t->executable = true;
        }
    }
    return 0;
}

static int wire_bun_validate(void)
{
    struct target targets[] = {
        {WIRE_BUN_EMIT, emit_code, sizeof emit_code - 1, false},
        {WIRE_BUN_DESTROY, destroy_code, sizeof destroy_code - 1, false},
        {WIRE_BUN_FREE, free_code, sizeof free_code - 1, false},
    };
    dl_iterate_phdr(mapped_targets, targets);
    for (size_t i = 0; i < sizeof targets / sizeof targets[0]; ++i)
        if (!targets[i].executable
            || memcmp((const void *)targets[i].address, targets[i].code, targets[i].length))
            return 1;
    return 0;
}

typedef bool (*emit_fn)(const struct bun_string *, const struct bun_string *, uint32_t,
                        bool, const unsigned char **, size_t *, void **, void *);
typedef void (*release_fn)(void *);

/* Called synchronously from Bun's initialized JS thread. The result is copied
 * out of JSC's refcounted storage and belongs to wire_bun_free(). */
static int wire_bun_emit_module(const unsigned char *url, size_t url_length,
                         const unsigned char *source, size_t source_length,
                         unsigned char **output, size_t *output_length)
{
    *output = NULL;
    *output_length = 0;
    if (wire_bun_validate())
        return 1;
    const struct bun_string origin = {2, {0}, (uintptr_t)url | (UINT64_C(1) << 61), url_length};
    const struct bun_string text = {2, {0}, (uintptr_t)source | (UINT64_C(1) << 61), source_length};
    const unsigned char *bytes = NULL;
    size_t length = 0;
    void *handle = NULL;
    if (!((emit_fn)(uintptr_t)WIRE_BUN_EMIT)(&origin, &text, UINT32_MAX, true,
                                       &bytes, &length, &handle, NULL))
        return 2;
    if (!bytes || !length || !handle)
        abort(); /* A successful emitter must transfer one valid reference. */
    unsigned char *copy = malloc(length);
    if (copy)
        memcpy(copy, bytes, length);
    uint32_t *references = handle;
    if (!*references)
        abort();
    if (*references == 1) {
        ((release_fn)(uintptr_t)WIRE_BUN_DESTROY)(handle);
        ((release_fn)(uintptr_t)WIRE_BUN_FREE)(handle);
    } else
        --*references;
    if (!copy)
        return 3;
    *output = copy;
    *output_length = length;
    return 0;
}

static void wire_bun_free(void *bytes)
{
    free(bytes);
}

static napi_value fail(napi_env env, const char *message)
{
    napi_throw_error(env, NULL, message);
    return NULL;
}

static napi_value validate(napi_env env, napi_callback_info info)
{
    (void)info;
    if (wire_bun_validate())
        return fail(env, "private Bun instruction signature mismatch");
    napi_value result;
    if (napi_get_boolean(env, true, &result) != napi_ok)
        return fail(env, "cannot return cache-emitter validation result");
    return result;
}

static napi_value emit_module(napi_env env, napi_callback_info info)
{
    size_t argc = 3;
    napi_value argv[3];
    bool is_buffer = false;
    size_t origin_length = 0, source_length = 0;
    void *source;
    if (napi_get_cb_info(env, info, &argc, argv, NULL, NULL) != napi_ok || argc != 2
        || napi_get_value_string_utf8(env, argv[0], NULL, 0, &origin_length) != napi_ok
        || napi_is_buffer(env, argv[1], &is_buffer) != napi_ok || !is_buffer
        || napi_get_buffer_info(env, argv[1], &source, &source_length) != napi_ok)
        return fail(env, "emitModule requires an origin string and source Buffer");
    if (!origin_length || !source_length)
        return fail(env, "cache origin and source must not be empty");
    char *origin = malloc(origin_length + 1);
    if (!origin)
        return fail(env, "out of memory");
    if (napi_get_value_string_utf8(env, argv[0], origin, origin_length + 1, &origin_length) != napi_ok) {
        free(origin);
        return fail(env, "cannot read cache origin");
    }
    unsigned char *output = NULL;
    size_t output_length = 0;
    int status = wire_bun_emit_module((const unsigned char *)origin, origin_length,
                                    source, source_length, &output, &output_length);
    free(origin);
    if (status) {
        const char *errors[] = {"", "private Bun instruction signature mismatch",
                                "JSC rejected module source", "out of memory"};
        return fail(env, errors[status]);
    }
    napi_value result;
    napi_status copied = napi_create_buffer_copy(env, output_length, output, NULL, &result);
    wire_bun_free(output);
    if (copied != napi_ok)
        return fail(env, "cannot copy generated JSC bytecode");
    return result;
}

NAPI_MODULE_INIT()
{
    const napi_property_descriptor properties[] = {
        {"validate", NULL, validate, NULL, NULL, NULL, napi_default, NULL},
        {"emitModule", NULL, emit_module, NULL, NULL, NULL, napi_default, NULL},
    };
    if (napi_define_properties(env, exports, sizeof properties / sizeof properties[0], properties) != napi_ok)
        return fail(env, "cannot initialize private Bun cache emitter");
    return exports;
}
#endif
