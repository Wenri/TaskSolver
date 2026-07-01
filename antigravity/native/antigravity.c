/* antigravity.c — LD_PRELOAD shim: embed frida-gum, install inline hooks on the
 * recovered Go function addresses, and forward events to the Python worker.
 *
 * Loaded into `agy` via LD_PRELOAD. The constructor (agy_init) verifies the
 * binary's build-id, starts the CPython worker, then installs gum hooks. All
 * heavy work happens in Python; the hook bodies here are deliberately tiny
 * because they run on goroutine stacks (see README).
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <link.h>
#include <elf.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "frida-gum.h"
#include "pybridge.h"
#include "symbols_gen.h"

/* ---- hook table generated from hooks.def ---------------------------------- */
enum {
#define HOOK(ID, NAME, MODE, KIND, STAGE, LEAVE) HK_##ID,
#include "hooks.def"
#undef HOOK
    HK_COUNT
};
static const struct { const char *name; agy_mode_t mode; const char *kind; int stage; int leave; } HOOKS[] = {
#define HOOK(ID, NAME, MODE, KIND, STAGE, LEAVE) [HK_##ID] = { NAME, MODE, KIND, STAGE, LEAVE },
#include "hooks.def"
#undef HOOK
};

/* ---- logging -------------------------------------------------------------- */
static FILE *g_logf;
#define LOG(...) do { FILE *f = g_logf ? g_logf : stderr; \
    fprintf(f, "[antigravity] " __VA_ARGS__); fputc('\n', f); fflush(f); } while (0)

static int g_tls_write_sync;   /* AGY_HOOK_TLS_WRITE_SYNC=1 → allow modifying egress */
static int g_dryrun;           /* AGY_HOOK_DRYRUN=1 → hooks fire but do nothing (isolate gum vs emit) */

/* ---- build-id of the main executable (via PT_NOTE, no file IO) ------------ */
struct bid { char hex[80]; int done; };
static int bid_cb(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    struct bid *b = data;
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

static void on_enter(GumInvocationContext *ic, gpointer user_data)
{
    (void)user_data;
    if (g_dryrun) return;
    int id = (int)(gsize)gum_invocation_context_get_listener_function_data(ic) - 1;
    GumCpuContext *cpu = ic->cpu_context;
    switch (id) {
    case HK_SMOKE_GETENV: {
        /* os.Getenv(key string): key.ptr=RAX, key.len=RBX — send the key so we
         * can confirm real string data flows through to Python. */
        agy_event_t ev = { .kind = "smoke", .stream_id = 0,
                           .data = (const uint8_t *)cpu->rax, .len = (size_t)cpu->rbx,
                           .mode = AGY_ASYNC };
        agy_py_emit(&ev);
        break;
    }
    case HK_TLS_WRITE: {
        /* crypto/tls.(*Conn).Write(c=RAX, b.ptr=RBX, b.len=RCX, b.cap=RDI) */
        uint64_t conn = cpu->rax, ptr = cpu->rbx, len = cpu->rcx;
        agy_event_t ev = { .kind = "tls_write", .stream_id = conn,
                           .data = (const uint8_t *)ptr, .len = (size_t)len,
                           .mode = g_tls_write_sync ? AGY_SYNC : AGY_ASYNC };
        agy_py_emit(&ev);
        if (ev.verdict && ev.out_data && ev.out_len <= len) {
            memcpy((void *)ptr, ev.out_data, ev.out_len);
            cpu->rcx = ev.out_len;   /* shrink the slice length the callee sees */
        }
        agy_py_free(&ev);
        break;
    }
    case HK_TLS_READ: {
        /* capture buffer ptr now; data is filled by the time we return */
        struct rd_state *s = gum_invocation_context_get_listener_invocation_data(ic, sizeof(*s));
        s->conn = cpu->rax;
        s->ptr  = cpu->rbx;
        break;
    }
    case HK_TLS_DECRYPT: {
        /* stash *halfConn receiver for stream correlation; plaintext is the return */
        struct rd_state *s = gum_invocation_context_get_listener_invocation_data(ic, sizeof(*s));
        s->conn = cpu->rax;
        s->ptr = 0;
        break;
    }
    case HK_HTTP_RT: {
        /* net/http.(*Transport).RoundTrip(t=RAX, req=RBX): use req ptr as id */
        agy_event_t ev = { .kind = "http_rt", .stream_id = cpu->rbx, .mode = AGY_ASYNC };
        agy_py_emit(&ev);
        break;
    }
    case HK_H2_PIPE_WRITE: {
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
    if (g_dryrun) return;
    int id = (int)(gsize)gum_invocation_context_get_listener_function_data(ic) - 1;
    GumCpuContext *cpu = ic->cpu_context;
    if (id == HK_TLS_READ) {
        int64_t n = (int64_t)cpu->rax;   /* return value: bytes read */
        struct rd_state *s = gum_invocation_context_get_listener_invocation_data(ic, sizeof(*s));
        if (n > 0 && s->ptr) {
            agy_event_t ev = { .kind = "tls_read", .stream_id = s->conn,
                               .data = (const uint8_t *)s->ptr, .len = (size_t)n,
                               .mode = AGY_ASYNC };
            agy_py_emit(&ev);
        }
    } else if (id == HK_TLS_DECRYPT) {
        /* (*halfConn).decrypt returns ([]byte plaintext, recordType, error):
         * RAX=ptr, RBX=len. This is a decrypted inbound TLS record = HTTP/2 frames
         * of the response. Safe on_leave: decrypt is CPU-only and doesn't park. */
        struct rd_state *s = gum_invocation_context_get_listener_invocation_data(ic, sizeof(*s));
        uint64_t ptr = cpu->rax, len = cpu->rbx;
        if (ptr && (int64_t)len > 0 && len < (16u << 20)) {
            agy_event_t ev = { .kind = HOOKS[id].kind, .stream_id = s->conn,
                               .data = (const uint8_t *)ptr, .len = (size_t)len,
                               .mode = AGY_ASYNC };
            agy_py_emit(&ev);
        }
    } else if (id == HK_SER_ROOT || id == HK_MAR_PROMPT || id == HK_PROTO_MARSHAL) {
        /* Go []byte return: RAX=ptr, RBX=len (CPU-only funcs). proto.Marshal is
         * hot; skip tiny protos to cut noise (the model request is large). */
        uint64_t ptr = cpu->rax, len = cpu->rbx;
        uint64_t minlen = (id == HK_PROTO_MARSHAL) ? 256 : 1;
        if (ptr && len >= minlen && len < (16u << 20)) {
            agy_event_t ev = { .kind = HOOKS[id].kind, .stream_id = 0,
                               .data = (const uint8_t *)ptr, .len = (size_t)len,
                               .mode = AGY_ASYNC };
            agy_py_emit(&ev);
        }
    }
}

static void install_hooks(int stage)
{
    gum_init_embedded();
    GumInterceptor *interceptor = gum_interceptor_obtain();
    /* Two listeners: enter-only avoids return-address rewriting (safer for Go's
     * stack unwinder); full is used only where we need the return value. */
    GumInvocationListener *l_enter = gum_make_call_listener(on_enter, NULL, NULL, NULL);
    GumInvocationListener *l_full  = gum_make_call_listener(on_enter, on_leave, NULL, NULL);
    GumModule *mainmod = gum_process_get_main_module();
    GumAddress base = gum_module_get_range(mainmod)->base_address;
    LOG("main module base = 0x%llx", (unsigned long long)base);

    gum_interceptor_begin_transaction(interceptor);
    for (int i = 0; i < HK_COUNT; i++) {
        /* AGY_HOOK_STAGE selects EXACTLY that stage's hooks (isolates each group) */
        if (HOOKS[i].stage != stage) continue;
        uint64_t va = agy_sym(HOOKS[i].name);
        if (!va) { LOG("symbol not found in map: %s", HOOKS[i].name); continue; }
        GumAttachOptions opt = { 0 };
        opt.listener_function_data = GSIZE_TO_POINTER((gsize)(i + 1));
        /* Attach PAST the stack-check prologue (agy_skip): Go's morestack re-runs
         * the real entry, not our trampoline. Args are still in registers at the
         * post-prologue point (before they're spilled). NOTE: this only fixes the
         * morestack hazard — a hooked function that PARKS the goroutine (tls_read,
         * RoundTrip, pipe.Write) still stalls agy even past-prologue, because gum's
         * per-thread return tracking breaks when Go resumes it on another OS thread.
         * Only hook functions that don't park (tls_write, halfConn.decrypt, CPU funcs). */
        uint64_t skip = agy_skip(HOOKS[i].name);
        gpointer addr = GSIZE_TO_POINTER((gsize)base + va + skip);
        GumInvocationListener *lis = HOOKS[i].leave ? l_full : l_enter;
        GumAttachReturn r = gum_interceptor_attach(interceptor, addr, lis, &opt);
        LOG("attach %-34s @ %p  (%s%s)  ret=%d", HOOKS[i].name, addr,
            HOOKS[i].mode == AGY_SYNC ? "sync" : "async",
            HOOKS[i].leave ? ",leave" : "", (int)r);
    }
    gum_interceptor_end_transaction(interceptor);
}

/* ---- libc interposer: cgo DNS (fires when Go uses the cgo resolver) --------
 * addrinfo is opaque here — we only read `node` and pass the rest through, so we
 * avoid pulling <netdb.h> (and the kernel UAPI headers it needs). */
struct addrinfo;
__attribute__((visibility("default")))
int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res)
{
    static int (*real)(const char *, const char *, const struct addrinfo *, struct addrinfo **);
    if (!real) real = dlsym(RTLD_NEXT, "getaddrinfo");
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
    if (!getenv("AGY_HOOK_ENABLE")) return;          /* opt-in */
    if (getenv("_AGY_SBOXSERVE")) return;            /* skip sandbox-server children */

    const char *logpath = getenv("AGY_HOOK_LOG");
    if (logpath && *logpath) g_logf = fopen(logpath, "ae");
    g_tls_write_sync = getenv("AGY_HOOK_TLS_WRITE_SYNC") != NULL;
    g_dryrun = getenv("AGY_HOOK_DRYRUN") != NULL;

    int stage = 3;
    const char *st = getenv("AGY_HOOK_STAGE");
    if (st && *st) stage = atoi(st);

    /* build-id guard: refuse to apply offsets to a different agy build */
    struct bid b = { .hex = "" };
    dl_iterate_phdr(bid_cb, &b);
    /* Require an EXACT build-id match. Missing/mismatched → skip (else we'd try to
     * hook agy offsets in the wrong binary — e.g. a preloaded child — and crash). */
    if (strcmp(b.hex, AGY_BUILD_ID) != 0) {
        LOG("build-id not agy (running=%s symbols=%s); not hooking this process",
            b.hex[0] ? b.hex : "<none>", AGY_BUILD_ID);
        if (!getenv("AGY_HOOK_FORCE")) return;
    } else {
        LOG("build-id ok (%s), stage=%d", b.hex, stage);
    }

    if (stage >= 1) {
        if (agy_py_start() != 0) { LOG("python bridge failed to start; not installing hooks"); return; }
    }
    if (stage >= 2) {
        install_hooks(stage);
    }
    LOG("initialized");
}
