/* antigravity.cpp — host executable for `libagy.so`: splice the synthetic Go
 * `moduledata` covering `agy_trampolines` onto `agy_firstmoduledata.next`,
 * start the embedded CPython worker (when `AGY_PROC_ENABLE=1`), enable the
 * `ld`-bound static trampoline stubs, and transfer control to `agy_rt0_go(argc, argv)`.
 *
 * Linked directly against `libagy.so` as its first DT_NEEDED dependency (with
 * no PT_TLS segment of its own, so `ld-linux-x86-64.so.2` assigns `libagy.so`
 * static TLS slot 1 at [%fs:-0x4a0 .. %fs:0)). Both directions of symbol binding
 * (`libagy.so` -> `agy_stub__*` in `vendor/agy` and `vendor/agy` -> `agy_go__*`,
 * `agy_firstmoduledata`, `agy_rt0_go` in `libagy.so`) are resolved at load time
 * by `ld-linux-x86-64.so.2` via `R_X86_64_GLOB_DAT` relocations.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <dlfcn.h>
#include <link.h>
#include <elf.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "wirecap.h"
#include "symbols_gen.h"
#include "gocall.h"
#include "procdef.h"
#include <cstddef>
#include "goframe.h"
#include "gomod.h"

static_assert(BK_SLOTS <= AGY_GOMOD_MAX_SLOTS,
              "more hooks than the static synthetic-moduledata holds — bump AGY_GOMOD_MAX_SLOTS in gomod.h");

/* ---- logging -------------------------------------------------------------- */
static FILE *g_logf;
#define LOG(...) do { FILE *f = g_logf ? g_logf : stderr; \
    std::fprintf(f, "[antigravity] " __VA_ARGS__); std::fputc('\n', f); std::fflush(f); } while (0)

static bool g_conv_id;   /* AGY_PROC_CONV_ID=1 → enable the os.OpenFile conversation-id probe */

extern "C" void agy_rt0_go(int argc, char **argv);

/* ---- build-id + load base of libagy.so (via PT_NOTE, no file IO) ---------- */
struct bid { char hex[80]; uint64_t base; int done; };
static int bid_cb(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    struct bid *b = (struct bid *)data;
    if (b->done) return 0;
    if (!info->dlpi_name || !std::strstr(info->dlpi_name, "libagy.so")) return 0;
    b->base = (uint64_t)info->dlpi_addr;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type != PT_NOTE) continue;
        const unsigned char *p = (const unsigned char *)(info->dlpi_addr + ph->p_vaddr);
        const unsigned char *end = p + ph->p_memsz;
        while (p + 12 <= end) {
            uint32_t namesz = *(const uint32_t *)p;
            uint32_t descsz = *(const uint32_t *)(p + 4);
            uint32_t type   = *(const uint32_t *)(p + 8);
            const unsigned char *name = p + 12;
            const unsigned char *desc = name + ((namesz + 3) & ~3u);
            if (type == NT_GNU_BUILD_ID && namesz == 4 && std::memcmp(name, "GNU", 3) == 0) {
                char *o = b->hex;
                for (uint32_t k = 0; k < descsz && k < 32; k++)
                    o += std::sprintf(o, "%02x", desc[k]);
                b->done = 1;
                return 1;
            }
            p = desc + ((descsz + 3) & ~3u);
        }
    }
    b->done = 1;   /* libagy.so had no build-id note */
    return 1;
}

/* ---- libc interposer: cgo DNS (fires when Go uses the cgo resolver) -------- */
struct addrinfo;
extern "C" __attribute__((visibility("default")))
int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res)
{
    static int (*real)(const char *, const char *, const struct addrinfo *, struct addrinfo **);
    if (!real) real = (int (*)(const char *, const char *, const struct addrinfo *,
                               struct addrinfo **))dlsym(RTLD_NEXT, "getaddrinfo");
    int rc = real(node, service, hints, res);
    if (wire_ready() && node) {
        wire_event_t ev = { .kind = "dns", .data = (const uint8_t *)node,
                           .len = std::strlen(node), .mode = WIRE_ASYNC };
        wire_emit(&ev);
    }
    return rc;
}

/* ---- initialization + entrypoint ------------------------------------------ */
static void agy_init(void)
{
    /* Register the synthetic Go moduledata covering `agy_trampolines` unconditionally
     * before Go starts, since `libagy.so` statically jumps through `agy_stub__*`. */
    /* The stubs tile the section by alignment, so a stub that outgrew its slot shows up here as
     * a section wider than nslots * stride. Nothing earlier can catch it (see goframe.cpp). */
    const size_t tramp_bytes = (size_t)(agy_tramp_region_end - agy_tramp_region_start);
    if (tramp_bytes != (size_t)BK_SLOTS * AGY_SLOT_SIZE) {
        std::fprintf(stderr, "[antigravity] FATAL: trampoline region is %zu bytes, expected %d*%d;"
                             " a stub body outgrew its slot\n",
                     tramp_bytes, BK_SLOTS, AGY_SLOT_SIZE);
        std::abort();
    }
    agy_gomod_register((uintptr_t)agy_tramp_region_start,
                       (uintptr_t)agy_tramp_region_end,
                       AGY_SLOT_SIZE, BK_SLOTS, AGY_FRAME, AGY_FRAME_LO);

    if (!std::getenv("AGY_PROC_ENABLE")) return;          /* opt-in */
    if (std::getenv("_AGY_SBOXSERVE")) return;            /* skip sandbox-server children */

    const char *logpath = std::getenv("AGY_PROC_LOG");
    if (logpath && *logpath) g_logf = std::fopen(logpath, "ae");
    g_conv_id = std::getenv("AGY_PROC_CONV_ID") != nullptr;
    agy_set_real_exe(std::getenv("AGY_PROC_REAL_EXE"));

    /* build-id guard: refuse to enable hooks on a different libagy.so build */
    struct bid b = { .hex = "", .base = 0, .done = 0 };
    dl_iterate_phdr(bid_cb, &b);
    if (!b.base || std::strcmp(b.hex, AGY_BUILD_ID) != 0) {
        LOG("build-id not agy (running=%s symbols=%s); not hooking this process",
            b.hex[0] ? b.hex : "<none>", AGY_BUILD_ID);
        if (!b.base || !std::getenv("AGY_PROC_FORCE")) return;
    } else {
        LOG("build-id ok (%s)", b.hex);
    }

    if (wire_start() != 0) { LOG("python bridge failed to start; not enabling hooks"); return; }
    LOG("main module base = 0x%llx", (unsigned long long)b.base);
    agy_enable_hooks(b.base, g_conv_id);
    LOG("initialized");
}

__attribute__((visibility("default")))
int main(int argc, char **argv)
{
    agy_init();
    agy_rt0_go(argc, argv);
    return 0;
}
