#define _GNU_SOURCE
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

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
    ((convert_fn)(uintptr_t)0x2c55230)(&impl, &input);
    return (struct bun_string){1, {0}, (uintptr_t)impl, 0};
}
#else
#include <link.h>
#include <stdlib.h>
#include <string.h>
#define NAPI_VERSION 8
#include <node_api.h>

/* Claude 2.1.316's private Bun 71c9bf0c, ET_EXEC. The C++ entry points are
 * stripped, so validate their instructions before calling their C ABI.
 * ZigSourceProvider.cpp owns generation; CachedBytecode's refcount release is
 * inlined in the Rust caller at 0x7b3e7d9 (destructor followed by mi_free).
 */
struct target {
    uintptr_t address;
    const unsigned char *code;
    size_t length;
    bool executable;
};

static const unsigned char emit_code[] =
    "\x55\x48\x89\xe5\x48\x83\xec\x20\x4c\x89\xc0\x41\x89\xc8\x89\xd1"
    "\x0f\x28\x45\x10\x0f\x11\x44\x24\x08\x4c\x89\x0c\x24\xba\x01\x00"
    "\x00\x00\x49\x89\xc1\xe8\x06\x00\x00\x00\x48\x83\xc4\x20\x5d\xc3";
static const unsigned char destroy_code[] =
    "\x55\x48\x89\xe5\x41\x57\x41\x56\x53\x50\x48\x89\xfb\x8b\x47\x64"
    "\x4c\x8b\x77\x58\x48\x85\xc0\x75\x2d\x83\x7b\x60\x00\x78\x78\x4d";
static const unsigned char free_code[] =
    "\x55\x48\x89\xe5\x48\x89\xfe\x48\x8b\x05\x42\xa4\x22\x06\x48\x89"
    "\xf9\x48\xc1\xe9\x1d\x48\x8b\x44\xc8\x50\x89\xf1\xc1\xe9\x0d\x81";

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
        {0x7b4f070, emit_code, sizeof emit_code - 1, false},
        {0x2e83be0, destroy_code, sizeof destroy_code - 1, false},
        {0x2a6dd30, free_code, sizeof free_code - 1, false},
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
    if (!((emit_fn)(uintptr_t)0x7b4f070)(&origin, &text, UINT32_MAX, true,
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
        ((release_fn)(uintptr_t)0x2e83be0)(handle);
        ((release_fn)(uintptr_t)0x2a6dd30)(handle);
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
