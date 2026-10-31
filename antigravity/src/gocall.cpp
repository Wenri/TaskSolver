/* gocall.cpp — static cgocall-trampoline hooks, per-hook C handlers (`agy_on_*`),
 * and the frame-pointer stack unwinder.
 *
 * Every trampoline stub (`agy_stub__<ID>`) is assembled at build time into
 * `agy_trampolines` (section) (emitted from `src/goframe.cpp` +
 * `symbols_gen.h`) with all Go targets (`agy_go__*`), gateways (`runtime.cgocall`,
 * `runtime.asmcgocall`), and per-hook C handlers (`agy_on_*`) bound by `ld`.
 * `libagy.so` statically patches each hooked Go function's `entry + skip` with
 * `jmp *agy_tramp_slots[slot](%rip)` (`FF 25 <disp32>`) and imports `agy_stub__<ID>`
 * via `R_X86_64_GLOB_DAT`, so `ld-linux-x86-64.so.2` binds both directions at
 * process load time with zero runtime code patching or `mprotect`.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "gomod.h"
#include "gocall.h"
#include "procdef.h"
#include "wirecap.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <unistd.h>
#include <sys/uio.h>
#include <array>
#include <string_view>

#define GHLOG(...) do { std::fprintf(stderr, "[antigravity/gohook] " __VA_ARGS__); \
                        std::fputc('\n', stderr); std::fflush(stderr); } while (0)

AGY_ASM_REFERENCED uint8_t g_agy_enabled = 0;
AGY_ASM_REFERENCED uint8_t g_agy_conv_id = 0;

long agy_safe_read(uint64_t addr, void *dst, unsigned long n)
{
    if (addr < 0x1000) return -1;
    struct iovec local = { dst, n };
    struct iovec remote = { (void *)(uintptr_t)addr, n };
    return process_vm_readv(getpid(), &local, 1, &remote, 1, 0);
}

int agy_backtrace(uint64_t rbp, uint64_t base, uint64_t *out, int max)
{
    int n = 0;
    uint64_t prev = 0;
    for (int i = 0; i < max; i++) {
        if (rbp < 0x10000 || (prev && rbp <= prev)) break;
        uint64_t frame[2];                       /* [0]=saved rbp, [1]=return pc */
        if (agy_safe_read(rbp, frame, 16) != 16) break;
        if (frame[1] < 0x1000) break;
        out[n++] = frame[1] - base;
        prev = rbp;
        rbp = frame[0];
    }
    return n;
}

void agy_emit_stack(const char *src_kind, uint64_t rbp, uint64_t base)
{
    std::array<uint64_t, 48> frames;
    int n = agy_backtrace(rbp, base, frames.data(), (int)frames.size());
    if (n <= 0) return;

    uint64_t h = 1469598103934665603ULL ^ (uint64_t)(uintptr_t)src_kind;
    for (int i = 0; i < n; i++) { h ^= frames[i]; h *= 1099511628211ULL; }
    static std::array<uint64_t, 1024> seen;
    static int nseen;
    for (int i = 0; i < nseen; i++) if (seen[i] == h) return;
    if (nseen < (int)seen.size()) seen[nseen++] = h; else return;

    unsigned char buf[64 + 48 * 8];
    size_t kl = std::strlen(src_kind);
    if (kl > 48) kl = 48;
    std::memcpy(buf, src_kind, kl);
    buf[kl] = 0;
    size_t off = kl + 1;
    std::memcpy(buf + off, frames.data(), (size_t)n * 8);
    off += (size_t)n * 8;
    wire_event_t ev = { .kind = "callstack", .stream_id = rbp,
                       .data = buf, .len = off, .mode = WIRE_ASYNC };
    wire_emit(&ev);
}

static bool mostly_printable(const unsigned char *p, size_t n)
{
    size_t ok = 0;
    for (size_t i = 0; i < n; i++)
        if (p[i] == '\t' || p[i] == '\n' || (p[i] >= 0x20 && p[i] < 0x7f)) ok++;
    return ok * 10 >= n * 8;
}

static void cgt_diag_append_string(char *rep, size_t cap, size_t *o,
                                    const char *label, uint64_t ptr, uint64_t len)
{
    if (*o >= cap || len == 0 || len > 4096) return;
    unsigned char tmp[256];
    size_t n = len < sizeof(tmp) ? (size_t)len : sizeof(tmp);
    if (agy_safe_read(ptr, tmp, n) != (ssize_t)n) return;
    if (!mostly_printable(tmp, n)) return;
    *o += std::snprintf(rep + *o, cap - *o, "  %s(len=%llu)=\"", label,
                   (unsigned long long)len);
    for (size_t i = 0; i < n && *o < cap - 2; i++)
        rep[(*o)++] = (tmp[i] >= 0x20 && tmp[i] < 0x7f) ? (char)tmp[i] :
                      (tmp[i] == '\n' ? ' ' : '.');
    if (*o < cap - 2) rep[(*o)++] = '"';
    if (*o < cap - 2) rep[(*o)++] = '\n';
    rep[*o] = 0;
}

struct walkctx { char *rep; size_t cap; size_t *o; int budget; std::array<uint64_t, 1024> seen; int nseen; };

static int cgt_seen(struct walkctx *c, uint64_t a)
{
    for (int i = 0; i < c->nseen; i++) if (c->seen[i] == a) return 1;
    if (c->nseen < (int)c->seen.size()) c->seen[c->nseen++] = a;
    return 0;
}

static void cgt_walk(struct walkctx *c, uint64_t addr, int depth, const char *path)
{
    if (c->budget <= 0 || addr < 0x10000 || *c->o > c->cap - 400) return;
    if (cgt_seen(c, addr)) return;
    unsigned char buf[64];
    c->budget--;
    if (agy_safe_read(addr, buf, sizeof(buf)) != (ssize_t)sizeof(buf)) return;
    for (int w = 0; w + 1 < 8; w++) {
        uint64_t p, l;
        std::memcpy(&p, buf + w * 8, 8);
        std::memcpy(&l, buf + (w + 1) * 8, 8);
        char lbl[72];
        std::snprintf(lbl, sizeof(lbl), "%s+%d", path, w * 8);
        cgt_diag_append_string(c->rep, c->cap, &*c->o, lbl, p, l);
    }
    if (depth <= 0) return;
    for (int w = 0; w < 8; w++) {
        uint64_t p;
        std::memcpy(&p, buf + w * 8, 8);
        if (p > 0x10000 && p != addr) {
            char np[72];
            std::snprintf(np, sizeof(np), "%s+%d", path, w * 8);
            cgt_walk(c, p, depth - 1, np);
        }
    }
}

static void cgt_diag(agy_block *b)
{
    char rep[16384];
    size_t o = 0;
    const char *ds = std::getenv("AGY_PROC_CGT_DEPTH");
    const char *bs = std::getenv("AGY_PROC_CGT_BUDGET");
    int depth = ds ? std::atoi(ds) : 3;
    int budget = bs ? std::atoi(bs) : 220;
    uint64_t r[10] = { b->regs.rax, b->regs.rbx, b->regs.rcx, b->regs.rdi, b->regs.rsi,
                       b->regs.r8, b->regs.r9, b->regs.r10, b->regs.r11, b->regs.rdx };
    static const char *nm[10] = { "rax", "rbx", "rcx", "rdi", "rsi",
                                  "r8", "r9", "r10", "r11", "rdx" };
    o += std::snprintf(rep + o, sizeof(rep) - o, "kind=%s recv=0x%llx\n",
                  (const char *)b->kind, (unsigned long long)r[0]);
    for (int i = 0; i < 10 && o < sizeof(rep) - 80; i++) {
        unsigned char s[24];
        char asc[25];
        int got = agy_safe_read(r[i], s, sizeof(s)) == (ssize_t)sizeof(s);
        if (got) {
            for (size_t k = 0; k < sizeof(s); k++)
                asc[k] = (s[k] >= 0x20 && s[k] < 0x7f) ? (char)s[k] : '.';
            asc[sizeof(s)] = 0;
        }
        o += std::snprintf(rep + o, sizeof(rep) - o, " %s=0x%llx%s%s\n", nm[i],
                      (unsigned long long)r[i], got ? " ~" : "", got ? asc : "");
    }
    for (int i = 1; i < 9; i++)
        cgt_diag_append_string(rep, sizeof(rep), &o, nm[i], r[i], r[i + 1]);
    struct walkctx c = { rep, sizeof(rep), &o, budget, {0}, 0 };
    for (int i = 0; i <= 4; i++)
        cgt_walk(&c, r[i], depth, nm[i]);
    wire_event_t ev = { .kind = "cgt_args", .stream_id = r[0],
                       .data = (const uint8_t *)rep, .len = o, .mode = WIRE_ASYNC };
    wire_emit(&ev);
}

#define CGT_RESP_CAP 16384
static void cgt_response_emit(agy_block *b)
{
    uint64_t s = b->regs.rsi;
    if (s < 0x10000) return;
    uint64_t hdr[3];                                   /* +0x0, +0x8(ptr), +0x10(len) */
    if (agy_safe_read(s, hdr, sizeof(hdr)) != (ssize_t)sizeof(hdr)) return;
    uint64_t ptr = hdr[1], len = hdr[2];
    if (ptr < 0x10000 || len == 0 || len > (16u << 20)) return;
    char buf[CGT_RESP_CAP];
    size_t n = len < CGT_RESP_CAP ? (size_t)len : CGT_RESP_CAP;
    if (agy_safe_read(ptr, buf, n) != (ssize_t)n) return;
    if (!mostly_printable((const unsigned char *)buf, n)) return;
    wire_event_t ev = { .kind = "app_response", .stream_id = b->regs.rax,
                       .data = (const uint8_t *)buf, .len = n, .mode = WIRE_ASYNC };
    wire_emit(&ev);
}

static void cgt_bytes_emit(const char *kind, uint64_t id, uint64_t ptr, uint64_t len,
                           bool stream)
{
    if (ptr < 0x10000 || len == 0 || len > (16u << 20)) return;
    char buf[CGT_RESP_CAP];
    const uint64_t end = stream ? len : (len < CGT_RESP_CAP ? len : CGT_RESP_CAP);
    for (uint64_t off = 0; off < end; off += CGT_RESP_CAP) {
        size_t n = end - off < CGT_RESP_CAP ? (size_t)(end - off) : CGT_RESP_CAP;
        if (agy_safe_read(ptr + off, buf, n) != (long)n) return;
        wire_event_t ev = { .kind = kind, .stream_id = id,
                           .data = (const uint8_t *)buf, .len = n, .mode = WIRE_ASYNC };
        wire_emit(&ev);
    }
}

static uint64_t g_gh_base;   /* libagy.so load base, for reducing PCs to link vaddrs */
static bool g_cgt_args;      /* cached AGY_PROC_CGT_ARGS */
static bool g_cgt_stack;     /* cached AGY_PROC_STACK */

static char g_real_exe[4096];
static size_t g_real_exe_len;

void agy_set_real_exe(const char *p)
{
    if (!p || !*p) return;
    size_t n = std::strlen(p);
    if (n >= sizeof g_real_exe) n = sizeof g_real_exe - 1;
    std::memcpy(g_real_exe, p, n);
    g_real_exe[n] = 0;
    g_real_exe_len = n;
}

static bool mem_has(const char *h, size_t n, std::string_view needle)
{
    return !needle.empty() && std::string_view(h, n).find(needle) != std::string_view::npos;
}

static inline void cgt_common(agy_block *b)
{
    if (g_cgt_args) cgt_diag(b);
    if (g_cgt_stack)
        agy_emit_stack((const char *)b->kind, b->regs.rbp, g_gh_base);
}

extern "C" AGY_ASM_REFERENCED void agy_on_generic(agy_block *b)
{
    cgt_common(b);
    wire_event_t ev = { .kind = (const char *)b->kind,
                       .stream_id = b->regs.rax, .mode = WIRE_ASYNC };
    wire_emit(&ev);
}

extern "C" AGY_ASM_REFERENCED void agy_on_fh_update(agy_block *b)
{
    cgt_common(b);
    cgt_response_emit(b);
    wire_event_t ev = { .kind = (const char *)b->kind,
                       .stream_id = b->regs.rax, .mode = WIRE_ASYNC };
    wire_emit(&ev);
}

extern "C" AGY_ASM_REFERENCED void agy_on_resp(agy_block *b)
{
    cgt_common(b);
    cgt_bytes_emit((const char *)b->kind, b->regs.rax, b->regs.rbx, b->regs.rcx, true);
}

extern "C" AGY_ASM_REFERENCED void agy_on_tls_write(agy_block *b)
{
    cgt_common(b);
    cgt_bytes_emit((const char *)b->kind, b->regs.rax, b->regs.rbx, b->regs.rcx, true);
}

extern "C" AGY_ASM_REFERENCED void agy_on_file_open(agy_block *b)
{
    cgt_common(b);
    const char *p = (const char *)b->regs.rax;
    size_t len = (size_t)b->regs.rbx;
    if (p && len > 0 && len < 4096 &&
        (mem_has(p, len, "conversations/") || mem_has(p, len, "/brain/"))) {
        wire_event_t ev = { .kind = (const char *)b->kind, .stream_id = 0,
                           .data = (const uint8_t *)p, .len = len, .mode = WIRE_ASYNC };
        wire_emit(&ev);
    }
}

extern "C" AGY_ASM_REFERENCED void agy_on_http_rt(agy_block *b)
{
    cgt_common(b);
    wire_event_t rt = { .kind = (const char *)b->kind, .stream_id = b->regs.rbx, .mode = WIRE_ASYNC };
    wire_emit(&rt);
}

extern "C" AGY_ASM_REFERENCED void agy_on_exit(agy_block *b)
{
    cgt_common(b);
    wire_event_t ev = { .kind = (const char *)b->kind, .stream_id = b->regs.rax, .mode = WIRE_SYNC };
    wire_emit(&ev);
    wire_shutdown();
}

extern "C" AGY_ASM_REFERENCED void agy_on_resp_chunk(agy_block *b)
{
    cgt_common(b);
    cgt_bytes_emit((const char *)b->kind, b->regs.rax, b->regs.rax, b->regs.rbx, false);
}

extern "C" AGY_ASM_REFERENCED void agy_on_readlink_filter(agy_block *b)
{
    cgt_common(b);
    uint64_t nptr = b->regs.rax, nlen = b->regs.rbx;
    char nm[16];
    if (g_real_exe_len && nlen == 14 && agy_safe_read(nptr, nm, 14) == 14 &&
        std::memcmp(nm, "/proc/self/exe", 14) == 0) {
        b->regs.rax = (uint64_t)(uintptr_t)g_real_exe;
        b->regs.rbx = g_real_exe_len;
        b->regs.rcx = 0;
        b->regs.rdi = 0;
        b->action = 1;
        static int logged;
        if (!logged) {
            logged = 1;
            char kr[512];
            ssize_t n = readlink("/proc/self/exe", kr, sizeof kr - 1);
            kr[n > 0 ? n : 0] = 0;
            GHLOG("os.readlink(/proc/self/exe): kernel=%s -> RETURN %s", kr, g_real_exe);
        }
        wire_event_t ev = { .kind = (const char *)b->kind, .stream_id = nptr,
                           .data = (const uint8_t *)g_real_exe, .len = g_real_exe_len,
                           .mode = WIRE_ASYNC };
        wire_emit(&ev);
    }
}

void agy_enable_hooks(uint64_t base, bool conv_id)
{
    g_gh_base   = base;
    g_cgt_args  = std::getenv("AGY_PROC_CGT_ARGS") != nullptr;
    g_cgt_stack = std::getenv("AGY_PROC_STACK") != nullptr;

    int n_tramp = 0, n_asm = 0;
    for (int i = 0; i < HK_COUNT; i++) {
        const auto &h = HOOKS[i];
        if (h.mech != AGY_FULLCGO && h.mech != AGY_ASMCGO) continue;
        if (i == hk("FILE_OPEN") && !conv_id) continue;
        n_tramp++;
        n_asm += (h.mech == AGY_ASMCGO);
        GHLOG("cgo-trampoline %.*s @ +%u -> %p (ld-bound static slot %d)",
              (int)h.id.size(), h.id.data(), h.skip, (void *)h.stub, h.slot);
    }
    g_agy_conv_id = conv_id ? 1 : 0;
    g_agy_enabled = 1;
    std::fprintf(stderr, "[antigravity] cgocall-trampoline: installed %d/%d target(s) (%d asmcgo, %d full-cgo)\n",
                 n_tramp, n_tramp, n_asm, n_tramp - n_asm);
    std::fflush(stderr);
}
