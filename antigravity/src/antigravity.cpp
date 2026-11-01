/* antigravity.cpp — the host for libagy.so: register the stubs' moduledata, start the embedded
 * CPython worker when AGY_PROC_ENABLE is set, enable the books, then run agy_rt0_go.
 *
 * libagy.so is the first DT_NEEDED and this executable has no PT_TLS of its own, so ld.so gives
 * libagy.so static TLS slot 1 and binds both directions eagerly, before Go starts.
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

#include "gocall.h"
#include "goframe.h"
#include "gomod.h"
#include "symbols_gen.h"
#include "wirecap.h"
#include <cstddef>

/* ---- logging -------------------------------------------------------------- */
static FILE *g_logf;
#define LOG(...) do { FILE *f = g_logf ? g_logf : stderr; \
    std::fprintf(f, "[antigravity] " __VA_ARGS__); std::fputc('\n', f); std::fflush(f); } while (0)

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
    /* Register the linked stub ranges before Go starts, even when books are disabled. */
    if (agy_gomod_register(agy_stub_descriptors()) != 0) {
        std::fprintf(stderr, "[antigravity] FATAL: invalid trampoline metadata\n");
        std::abort();
    }

    if (!std::getenv("AGY_PROC_ENABLE")) return;          /* opt-in */
    if (std::getenv("_AGY_SBOXSERVE")) return;            /* skip sandbox-server children */

    const char *logpath = std::getenv("AGY_PROC_LOG");
    if (logpath && *logpath) g_logf = std::fopen(logpath, "ae");
    agy_set_real_exe(std::getenv("AGY_PROC_REAL_EXE"));

    /* build-id guard: refuse to enable books on a different libagy.so build */
    struct bid b = { .hex = "", .base = 0, .done = 0 };
    dl_iterate_phdr(bid_cb, &b);
    if (!b.base || std::strcmp(b.hex, AGY_BUILD_ID) != 0) {
        LOG("build-id not agy (running=%s symbols=%s); not booking this process",
            b.hex[0] ? b.hex : "<none>", AGY_BUILD_ID);
        if (!b.base || !std::getenv("AGY_PROC_FORCE")) return;
    } else {
        LOG("build-id ok (%s)", b.hex);
    }

    if (wire_start() != 0) { LOG("python bridge failed to start; not enabling books"); return; }
    LOG("main module base = 0x%llx", (unsigned long long)b.base);
    agy_enable_books(b.base, std::getenv("AGY_PROC_CONV_ID") != nullptr);
    LOG("initialized");
}

__attribute__((visibility("default")))
int main(int argc, char **argv)
{
    agy_init();
    agy_rt0_go(argc, argv);
    return 0;
}
