/* antigravity.cpp — host executable for `libagy.so`: embed frida-gum, install
 * inline cgocall-trampoline hooks on the recovered Go function addresses in
 * `libagy.so`, forward events to the Python worker, and transfer control to
 * `agy_rt0_go(argc, argv)`.
 *
 * Linked directly against `libagy.so` as its first DT_NEEDED dependency (with
 * no PT_TLS segment of its own, so `ld-linux-x86-64.so.2` assigns `libagy.so`
 * static TLS slot 1 at [%fs:-0x4a0 .. %fs:0)). `main()` runs `agy_init()` —
 * verifying `libagy.so`'s build-id, starting the CPython worker, and installing
 * trampoline hooks — before jumping into Go's `runtime.rt0_go`.
 */
#ifndef _GNU_SOURCE          /* g++ already defines it; guard avoids a redefinition warning */
#define _GNU_SOURCE
#endif
#include <dlfcn.h>
#include <link.h>
#include <elf.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "frida-gum.h"
#include "wirecap.h"
#include "symbols_gen.h"
#include "cgotrampoline.h"
#include "procdef.h"   /* agy_mech_t, the constexpr HOOKS[] table, HK_COUNT, and hk("id") lookup */
#include "gomod.h"     /* AGY_GOMOD_MAX_SLOTS — the static synthetic-moduledata's slot capacity */

/* The synthetic moduledata (gomod.cpp) holds its per-slot tables in a fixed static buffer sized for
 * AGY_GOMOD_MAX_SLOTS. Trampoline slots ≤ installed hooks ≤ HK_COUNT, so guarantee it fits at build. */
static_assert(HK_COUNT <= AGY_GOMOD_MAX_SLOTS,
              "more hooks than the static synthetic-moduledata holds — bump AGY_GOMOD_MAX_SLOTS in gomod.h");

/* ---- logging -------------------------------------------------------------- */
static FILE *g_logf;
#define LOG(...) do { FILE *f = g_logf ? g_logf : stderr; \
    std::fprintf(f, "[antigravity] " __VA_ARGS__); std::fputc('\n', f); std::fflush(f); } while (0)

static int g_tls_write_sync;   /* AGY_PROC_TLS_WRITE_SYNC=1 → allow modifying egress */
static int g_conv_id;          /* AGY_PROC_CONV_ID=1 → install the os.OpenFile conversation-id probe */
static uint64_t g_base;        /* libagy.so load base (for PC→link-vaddr reduction) */

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

static void install_hooks(uint64_t base)
{
    /* Still needed after the gum listener path was retired: the trampoline builder uses gum's
     * x86 code writer + its near-page allocator. */
    gum_init_embedded();
    g_base = base;       /* for agy_emit_stack PC→link-vaddr reduction */
    LOG("main module base = 0x%llx", (unsigned long long)base);

    /* Trampoline hooks (AGY_FULLCGO/AGY_ASMCGO): each target's Go function pointer (`HOOKS[i].fn`)
     * is bound directly by `ld` against `libagy.so`, and each hook's dedicated C callback
     * (`HOOKS[i].handler`) is baked directly into its trampoline stub. `finalize()` registers the
     * covering synthetic moduledata onto `ld`-bound `agy_firstmoduledata.next`. */
    {
        auto gh = AgyGoHook::begin(base, agy_fn("runtime.cgocall"),
                                   agy_fn("runtime.asmcgocall"), HK_COUNT);
        int n_tramp = 0, n_asm = 0, made = 0;
        if (gh) {
            for (int i = 0; i < HK_COUNT; i++) {
                if (HOOKS[i].mech != AGY_FULLCGO && HOOKS[i].mech != AGY_ASMCGO) continue;
                /* FILE_OPEN is an OVERLAY: only install it when the caller asked for
                 * conversation-id capture, so an ordinary run doesn't pay a cgocall on every
                 * os.OpenFile. */
                if (i == hk("FILE_OPEN") && !g_conv_id) continue;
                if (!HOOKS[i].fn) { LOG("symbol not found in map: %s", HOOKS[i].name); continue; }
                int asmcgo = (HOOKS[i].mech == AGY_ASMCGO);
                gh->add(HOOKS[i].fn, HOOKS[i].skip, HOOKS[i].kind, HOOKS[i].handler, asmcgo);
                n_tramp++; n_asm += asmcgo;
            }
            made = gh->finalize();
        }
        LOG("cgocall-trampoline: installed %d/%d target(s) (%d asmcgo, %d full-cgo)",
            made, n_tramp, n_asm, n_tramp - n_asm);
    }   /* gh (unique_ptr) frees here — releases the gum writer; trampolines + moduledata persist */

}

/* ---- libc interposer: cgo DNS (fires when Go uses the cgo resolver) --------
 * addrinfo is opaque here — we only read `node` and pass the rest through, so we
 * avoid pulling <netdb.h> (and the kernel UAPI headers it needs). */
struct addrinfo;
/* extern "C": this is a libc interposer resolved by the dynamic linker BY NAME, so it
 * must export the unmangled symbol `getaddrinfo` (C++ mangling would hide it). */
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
    if (!std::getenv("AGY_PROC_ENABLE")) return;          /* opt-in */
    if (std::getenv("_AGY_SBOXSERVE")) return;            /* skip sandbox-server children */

    const char *logpath = std::getenv("AGY_PROC_LOG");
    if (logpath && *logpath) g_logf = std::fopen(logpath, "ae");
    g_tls_write_sync = std::getenv("AGY_PROC_TLS_WRITE_SYNC") != nullptr;
    g_conv_id = std::getenv("AGY_PROC_CONV_ID") != nullptr;
    agy_set_real_exe(std::getenv("AGY_PROC_REAL_EXE"));   /* the path READLINK_FILTER returns for /proc/self/exe */

    /* build-id guard: refuse to apply offsets to a different libagy.so build */
    struct bid b = { .hex = "", .base = 0, .done = 0 };
    dl_iterate_phdr(bid_cb, &b);
    /* Require an EXACT build-id match. Missing/mismatched → skip (else we'd try to
     * hook agy offsets in the wrong binary and crash). */
    if (!b.base || std::strcmp(b.hex, AGY_BUILD_ID) != 0) {
        LOG("build-id not agy (running=%s symbols=%s); not hooking this process",
            b.hex[0] ? b.hex : "<none>", AGY_BUILD_ID);
        if (!b.base || !std::getenv("AGY_PROC_FORCE")) return;
    } else {
        LOG("build-id ok (%s)", b.hex);
    }

    /* Start the embedded Python bridge, then install the full working hook union. */
    if (wire_start() != 0) { LOG("python bridge failed to start; not installing hooks"); return; }
    install_hooks(b.base);
    LOG("initialized");
}

__attribute__((visibility("default")))
int main(int argc, char **argv)
{
    agy_init();
    agy_rt0_go(argc, argv);
    return 0;
}
