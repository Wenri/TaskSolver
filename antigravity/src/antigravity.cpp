/* antigravity.cpp — LD_PRELOAD shim: embed frida-gum, install inline hooks on the
 * recovered Go function addresses, and forward events to the Python worker.
 *
 * Loaded into `agy` via LD_PRELOAD. The constructor (agy_init) verifies the
 * binary's build-id, starts the CPython worker, then installs gum hooks. All
 * heavy work happens in Python; the hook bodies here are deliberately tiny
 * because they run on goroutine stacks (see README).
 */
#ifndef _GNU_SOURCE          /* g++ already defines it; guard avoids a redefinition warning */
#define _GNU_SOURCE
#endif
#include <dlfcn.h>
#include <link.h>
#include <elf.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string_view>

#include "frida-gum.h"
#include "pybridge.h"
#include "symbols_gen.h"
#include "cgotrampoline.h"
#include "procdef.h"   /* agy_mech_t, the constexpr HOOKS[] table, HK_COUNT, and hk("id") lookup */

/* ---- logging -------------------------------------------------------------- */
static FILE *g_logf;
#define LOG(...) do { FILE *f = g_logf ? g_logf : stderr; \
    fprintf(f, "[antigravity] " __VA_ARGS__); fputc('\n', f); fflush(f); } while (0)

static int g_tls_write_sync;   /* AGY_PROC_TLS_WRITE_SYNC=1 → allow modifying egress */
static int g_stack;            /* AGY_PROC_STACK=1 → emit a "callstack" event per hook fire */
static int g_conv_id;          /* AGY_PROC_CONV_ID=1 → install the os.OpenFile conversation-id probe */
static uint64_t g_base;        /* main-module base (for PC→link-vaddr reduction) */

/* ---- build-id of the main executable (via PT_NOTE, no file IO) ------------ */
struct bid { char hex[80]; int done; };
static int bid_cb(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    struct bid *b = (struct bid *)data;
    if (b->done) return 0;                 /* first object == main program */
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
            if (type == NT_GNU_BUILD_ID && namesz == 4 && memcmp(name, "GNU", 3) == 0) {
                char *o = b->hex;
                for (uint32_t k = 0; k < descsz && k < 32; k++)
                    o += sprintf(o, "%02x", desc[k]);
                b->done = 1;
                return 1;
            }
            p = desc + ((descsz + 3) & ~3u);
        }
    }
    b->done = 1;   /* main program had no build-id note */
    return 1;
}

/* ---- gum listener --------------------------------------------------------- */
struct rd_state { uint64_t conn, ptr; };  /* passed enter→leave for TLS_READ */

/* substring scan over a NON-nul-terminated Go string (name.ptr/name.len). The (ptr,len)
 * string_view never reads past n, and find() allocates nothing — safe on a goroutine stack. */
static bool mem_has(const char *h, size_t n, std::string_view needle)
{
    return !needle.empty() && std::string_view(h, n).find(needle) != std::string_view::npos;
}

static void on_enter(GumInvocationContext *ic, gpointer user_data)
{
    (void)user_data;
    int id = (int)(gsize)gum_invocation_context_get_listener_function_data(ic) - 1;
    GumCpuContext *cpu = ic->cpu_context;
    /* AGY_PROC_STACK: dump the call stack leading INTO this hook. gum fires
     * post-prologue, so cpu->rbp is the target's own frame → complete upward chain
     * (this is the exact function context around tls_write / decrypt). */
    if (g_stack) agy_emit_stack(HOOKS[id].kind, cpu->rbp, g_base);
    switch (id) {
    case hk("SMOKE_GETENV"): {
        /* os.Getenv(key string): key.ptr=RAX, key.len=RBX — send the key so we
         * can confirm real string data flows through to Python. */
        agy_event_t ev = { .kind = "smoke", .stream_id = 0,
                           .data = (const uint8_t *)cpu->rax, .len = (size_t)cpu->rbx,
                           .mode = AGY_ASYNC };
        agy_py_emit(&ev);
        break;
    }
    case hk("FILE_OPEN"): {
        /* os.OpenFile(name string, ...): name.ptr=RAX, name.len=RBX. agy's conversation
         * store lives at .../conversations/<uuid>.db and .../brain/<uuid>/.../transcript.jsonl,
         * so the uuid is IN the path. Filter to those paths HERE (C, cheap) so Python only
         * sees a conversation open, then it parses the uuid → a conversation_id event. */
        const char *p = (const char *)cpu->rax;
        size_t len = (size_t)cpu->rbx;
        if (p && len > 0 && len < 4096 &&
            (mem_has(p, len, "conversations/") || mem_has(p, len, "/brain/"))) {
            agy_event_t ev = { .kind = "file_open", .stream_id = 0,
                               .data = (const uint8_t *)p, .len = len, .mode = AGY_ASYNC };
            agy_py_emit(&ev);
        }
        break;
    }
    case hk("TLS_WRITE"): {
        /* crypto/tls.(*Conn).Write(c=RAX, b.ptr=RBX, b.len=RCX, b.cap=RDI) */
        uint64_t conn = cpu->rax, ptr = cpu->rbx, len = cpu->rcx;
        agy_event_t ev = { .kind = "tls_write", .stream_id = conn,
                           .data = (const uint8_t *)ptr, .len = (size_t)len,
                           .mode = g_tls_write_sync ? AGY_SYNC : AGY_ASYNC };
        agy_py_emit(&ev);   /* SYNC: on a rewrite the bridge already replaced ev.data (== ptr) in
                             * place; verdict says so, out_len is the new (equal-or-shorter) length. */
        if (ev.verdict) cpu->rcx = ev.out_len;   /* shrink the slice length the callee sees */
        agy_py_free(&ev);
        break;
    }
    case hk("TLS_READ"): {
        /* capture buffer ptr now; data is filled by the time we return */
        struct rd_state *s = (struct rd_state *)gum_invocation_context_get_listener_invocation_data(ic, sizeof(*s));
        s->conn = cpu->rax;
        s->ptr  = cpu->rbx;
        break;
    }
    case hk("TLS_DECRYPT"): {
        /* stash *halfConn receiver for stream correlation; plaintext is the return */
        struct rd_state *s = (struct rd_state *)gum_invocation_context_get_listener_invocation_data(ic, sizeof(*s));
        s->conn = cpu->rax;
        s->ptr = 0;
        break;
    }
    case hk("HTTP_RT"): {
        /* net/http.(*Transport).RoundTrip(t=RAX, req=RBX): use req ptr as id */
        agy_event_t ev = { .kind = "http_rt", .stream_id = cpu->rbx, .mode = AGY_ASYNC };
        agy_py_emit(&ev);
        break;
    }
    case hk("H2_PIPE_WRITE"): {
        /* http2 (*pipe).Write(p []byte): receiver=RAX, p.ptr=RBX, p.len=RCX.
         * The de-framed response body chunk (ingress) — data is the input arg,
         * valid at entry. CPU-only func, so hooking is safe (no park). */
        uint64_t pipe = cpu->rax, ptr = cpu->rbx, len = cpu->rcx;
        if (ptr && len && len < (16u << 20)) {
            agy_event_t ev = { .kind = "resp", .stream_id = pipe,
                               .data = (const uint8_t *)ptr, .len = (size_t)len,
                               .mode = AGY_ASYNC };
            agy_py_emit(&ev);
        }
        break;
    }
    default: break;
    }
}

static void on_leave(GumInvocationContext *ic, gpointer user_data)
{
    (void)user_data;
    int id = (int)(gsize)gum_invocation_context_get_listener_function_data(ic) - 1;
    GumCpuContext *cpu = ic->cpu_context;
    if (id == hk("TLS_READ")) {
        int64_t n = (int64_t)cpu->rax;   /* return value: bytes read */
        struct rd_state *s = (struct rd_state *)gum_invocation_context_get_listener_invocation_data(ic, sizeof(*s));
        if (n > 0 && s->ptr) {
            agy_event_t ev = { .kind = "tls_read", .stream_id = s->conn,
                               .data = (const uint8_t *)s->ptr, .len = (size_t)n,
                               .mode = AGY_ASYNC };
            agy_py_emit(&ev);
        }
    } else if (id == hk("TLS_DECRYPT")) {
        /* (*halfConn).decrypt returns ([]byte plaintext, recordType, error):
         * RAX=ptr, RBX=len. This is a decrypted inbound TLS record = HTTP/2 frames
         * of the response. Safe on_leave: decrypt is CPU-only and doesn't park. */
        struct rd_state *s = (struct rd_state *)gum_invocation_context_get_listener_invocation_data(ic, sizeof(*s));
        uint64_t ptr = cpu->rax, len = cpu->rbx;
        if (ptr && (int64_t)len > 0 && len < (16u << 20)) {
            agy_event_t ev = { .kind = HOOKS[id].kind, .stream_id = s->conn,
                               .data = (const uint8_t *)ptr, .len = (size_t)len,
                               .mode = AGY_ASYNC };
            agy_py_emit(&ev);
        }
    } else if (HOOKS[id].retcap > 0) {
        /* Return-[]byte/string leaf getters (retcap>0 in procdef.h): RAX=ptr, RBX=len (CPU-only
         * funcs). Gate on len >= retcap — e.g. 256 skips tiny protos on the hot proto.Marshal;
         * the delta/response getters return the assistant text as a Go string. (TLS_READ/DECRYPT
         * are retcap<0 — handled by the ID branches above, not here.) */
        uint64_t ptr = cpu->rax, len = cpu->rbx;
        if (ptr && len >= (uint32_t)HOOKS[id].retcap && len < (16u << 20)) {
            agy_event_t ev = { .kind = HOOKS[id].kind, .stream_id = 0,
                               .data = (const uint8_t *)ptr, .len = (size_t)len,
                               .mode = AGY_ASYNC };
            agy_py_emit(&ev);
        }
    }
}

static void install_hooks(void)
{
    gum_init_embedded();
    GumInterceptor *interceptor = gum_interceptor_obtain();
    /* Two listeners:
     *  - PROBE (enter-only, leave=0 hooks): a true fire-on-enter listener that installs
     *    NO return trampoline. gum_make_call_listener(on_enter, NULL) does NOT achieve
     *    this — it still intercepts the return (restores the clobbered return addr + pops
     *    its per-thread invocation context), which is what stalled the parking funcs.
     *    A probe listener leaves the return untouched, so the goroutine can park and
     *    resume on another OS thread without corrupting gum's bookkeeping.
     *  - CALL (enter+leave, leave=1 hooks): used only where we need the []byte return. */
    GumInvocationListener *l_enter = gum_make_probe_listener(on_enter, nullptr, nullptr);
    GumInvocationListener *l_full  = gum_make_call_listener(on_enter, on_leave, nullptr, nullptr);
    GumModule *mainmod = gum_process_get_main_module();
    GumAddress base = gum_module_get_range(mainmod)->base_address;
    g_base = (uint64_t)base;       /* for agy_emit_stack PC→link-vaddr reduction */
    LOG("main module base = 0x%llx", (unsigned long long)base);

    /* Trampoline hooks (AGY_FULLCGO/AGY_ASMCGO): the cgocall-trampoline path
     * (cgotrampoline.cpp) — NOT a gum attach. These are the parking scheduling-path funcs
     * (SendUserMessage/Send, the gemini_coder framework consumers, the CodeAssistClient
     * RPCs). Resolve + filter the union HERE and stream each into the builder — no
     * intermediate array. It's a SINGLE region + synthetic moduledata (the gomod.cpp
     * singletons make a second install unsafe), so all go through one begin/add/finalize. */
    {
        agy_gohook *gh = agy_gohook_begin((uint64_t)base, agy_sym("runtime.cgocall"),
                                          agy_sym("runtime.asmcgocall"), HK_COUNT);
        int n_tramp = 0, n_asm = 0;
        for (int i = 0; i < HK_COUNT; i++) {
            if (HOOKS[i].mech != AGY_FULLCGO && HOOKS[i].mech != AGY_ASMCGO) continue;
            uint64_t va = HOOKS[i].vaddr;
            if (!va) { LOG("symbol not found in map: %s", HOOKS[i].name); continue; }
            int asmcgo = (HOOKS[i].mech == AGY_ASMCGO);
            agy_gohook_add(gh, va, HOOKS[i].skip, HOOKS[i].kind, asmcgo);
            n_tramp++; n_asm += asmcgo;
        }
        int made = agy_gohook_finalize(gh, AGY_MODULEDATA_VADDR);
        LOG("cgocall-trampoline: installed %d/%d target(s) (%d asmcgo, %d full-cgo)",
            made, n_tramp, n_asm, n_tramp - n_asm);
    }

    /* AGY_GUM hooks: frida-gum inline attach on the non-parking CPU funcs. */
    gum_interceptor_begin_transaction(interceptor);
    for (int i = 0; i < HK_COUNT; i++) {
        if (HOOKS[i].mech != AGY_GUM) continue;
        /* FILE_OPEN is an OVERLAY: only install it when the caller asked for conversation-id
         * capture, so an ordinary run isn't burdened by os.OpenFile. */
        if (i == hk("FILE_OPEN") && !g_conv_id) continue;
        uint64_t va = HOOKS[i].vaddr;
        if (!va) { LOG("symbol not found in map: %s", HOOKS[i].name); continue; }
        GumAttachOptions opt = {};
        opt.listener_function_data = GSIZE_TO_POINTER((gsize)(i + 1));
        /* Attach PAST the stack-check prologue (agy_skip): Go's morestack re-runs
         * the real entry, not our trampoline. Args are still in registers at the
         * post-prologue point (before they're spilled). NOTE: this only fixes the
         * morestack hazard — a hooked function that PARKS the goroutine (tls_read,
         * RoundTrip, pipe.Write) still stalls agy even past-prologue, because gum's
         * per-thread return tracking breaks when Go resumes it on another OS thread.
         * Only hook functions that don't park (tls_write, halfConn.decrypt, CPU funcs). */
        uint64_t skip = HOOKS[i].skip;
        gpointer addr = GSIZE_TO_POINTER((gsize)base + va + skip);
        /* retcap != 0 → the return is intercepted, so use the enter+leave listener (a return-address
         * rewrite); retcap == 0 → the enter-only probe listener (no return trampoline). */
        GumInvocationListener *lis = HOOKS[i].retcap ? l_full : l_enter;
        GumAttachReturn r = gum_interceptor_attach(interceptor, addr, lis, &opt);
        LOG("attach %-34s @ %p  (%s%s)  ret=%d", HOOKS[i].name, addr,
            HOOKS[i].mode == AGY_SYNC ? "sync" : "async",
            HOOKS[i].retcap ? ",leave" : "", (int)r);
    }
    gum_interceptor_end_transaction(interceptor);
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
    if (agy_py_ready() && node) {
        agy_event_t ev = { .kind = "dns", .data = (const uint8_t *)node,
                           .len = strlen(node), .mode = AGY_ASYNC };
        agy_py_emit(&ev);
    }
    return rc;
}

/* ---- constructor ---------------------------------------------------------- */
__attribute__((constructor))
static void agy_init(void)
{
    if (!getenv("AGY_PROC_ENABLE")) return;          /* opt-in */
    if (getenv("_AGY_SBOXSERVE")) return;            /* skip sandbox-server children */

    const char *logpath = getenv("AGY_PROC_LOG");
    if (logpath && *logpath) g_logf = fopen(logpath, "ae");
    g_tls_write_sync = getenv("AGY_PROC_TLS_WRITE_SYNC") != nullptr;
    g_stack = getenv("AGY_PROC_STACK") != nullptr;
    g_conv_id = getenv("AGY_PROC_CONV_ID") != nullptr;

    /* build-id guard: refuse to apply offsets to a different agy build */
    struct bid b = { .hex = "" };
    dl_iterate_phdr(bid_cb, &b);
    /* Require an EXACT build-id match. Missing/mismatched → skip (else we'd try to
     * hook agy offsets in the wrong binary — e.g. a preloaded child — and crash). */
    if (strcmp(b.hex, AGY_BUILD_ID) != 0) {
        LOG("build-id not agy (running=%s symbols=%s); not hooking this process",
            b.hex[0] ? b.hex : "<none>", AGY_BUILD_ID);
        if (!getenv("AGY_PROC_FORCE")) return;
    } else {
        LOG("build-id ok (%s)", b.hex);
    }

    /* Start the embedded Python bridge, then install the full working hook union. */
    if (agy_py_start() != 0) { LOG("python bridge failed to start; not installing hooks"); return; }
    install_hooks();
    LOG("initialized");
}
