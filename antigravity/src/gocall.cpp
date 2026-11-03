/* gocall.cpp — the C side of the stubs. See gocall.h. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "gomod.h"
#include "gocall.h"
#include "wire_logline.h"
#include "procdef.h"
#include "symbols_gen.h"
#include "wirecap.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <unistd.h>
#include <sys/uio.h>
#include <array>
#include <atomic>
#include <memory>
#include <new>
#include <string_view>

#define GHLOG(...) wire_logline(STDERR_FILENO, "[antigravity/gobook] " __VA_ARGS__)

/* Null until agy_enable_books, which runs before Go starts. */
__attribute__((used, externally_visible)) agy_book_fn g_agy_on[BK_COUNT];

static uint64_t g_gh_base;   /* libagy.so load base */

static agy_book_fn agy_handler_for(std::string_view id) {
    if (id == "fh_update")       return &agy_on_fh_update;
    if (id == "resp" || id == "tls_write") return &agy_on_stream;
    if (id == "file_open")       return &agy_on_file_open;
    if (id == "http_rt")         return &agy_on_http_rt;
    if (id == "exit")            return &agy_on_exit;
    if (id == "resp_chunk")      return &agy_on_resp_chunk;
    if (id == "system_message" || id == "system_message_legacy")
        return &agy_on_system_message;
    if (id == "readlink_filter") return &agy_on_readlink_filter;
    return &agy_on_generic;
}

/* Events carry their row's id as the kind. */
static const char *book_id(const agy_block *b) { return BOOKS[b->book].id.data(); }

static void emit_book(agy_block *b, uint64_t stream_id, wire_mode_t mode = WIRE_ASYNC)
{
    wire_event_t ev = { .kind = book_id(b), .stream_id = stream_id, .mode = mode };
    wire_emit(&ev);
}

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
    // Producers run on multiple Go threads. Slots only transition from empty to a hash.
    static_assert(std::atomic<uint64_t>::is_always_lock_free);
    static std::array<std::atomic<uint64_t>, 1024> seen{};
    if (!h) h = 1;  // Zero is the empty-slot marker.
    bool inserted = false;
    for (auto &slot : seen) {
        uint64_t present = slot.load(std::memory_order_relaxed);
        if (present == h) return;
        if (!present && slot.compare_exchange_strong(present, h, std::memory_order_relaxed)) {
            inserted = true;
            break;
        }
        if (present == h) return;
    }
    if (!inserted) return;

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
                  book_id(b), (unsigned long long)r[0]);
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

/* App snapshots and SSE JSON lines must remain atomic. Original size lets the Python
 * decoder reject native truncation and a smaller WIRE_MAXCOPY bridge limit. */
static void cgt_response_packet(const char *kind, uint64_t id, uint64_t ptr, uint64_t len)
{
    if (ptr < 0x10000 || len > INT64_MAX) return;
    struct header { char magic[4]; uint32_t version; uint64_t size; };
    const header h = {{'A', 'G', 'T', 'X'}, 1, len};
    static_assert(sizeof(header) == 16);
    constexpr size_t limit = 1u << 20;
    const size_t copied = (size_t)std::min<uint64_t>(len, limit - sizeof(h));
    std::unique_ptr<uint8_t[]> packet(new (std::nothrow) uint8_t[sizeof(h) + copied]);
    if (!packet) return;
    std::memcpy(packet.get(), &h, sizeof(h));
    if (copied && agy_safe_read(ptr, packet.get() + sizeof(h), copied) != (long)copied) return;
    wire_event_t ev = {.kind = kind, .stream_id = id, .data = packet.get(),
                       .len = sizeof(h) + copied, .mode = WIRE_ASYNC};
    wire_emit(&ev);
}

static void cgt_response_emit(agy_block *b)
{
    /* Pinned Step.GetPlannerResponse and CortexStepPlannerResponse.GetResponse:
     * RDI is the Step. Type 15 is the planner response; +0x60 holds its oneof wrapper.
     * Step.Metadata.ToolCall is a different string and must never become the answer. */
    const uint64_t step = b->regs.rdi;
    uint32_t type = 0;
    uint64_t itab = 0, wrapper = 0, response = 0;
    uint64_t text[2];
    if (step < 0x10000 || step > UINT64_MAX - 0x68
            || agy_safe_read(step + 8, &type, sizeof(type)) != (long)sizeof(type) || type != 15
            || agy_safe_read(step + 0x58, &itab, sizeof(itab)) != (long)sizeof(itab)
            || itab != g_gh_base + AGY_PLANNER_RESPONSE_ITAB
            || agy_safe_read(step + 0x60, &wrapper, sizeof(wrapper)) != (long)sizeof(wrapper)
            || agy_safe_read(wrapper, &response, sizeof(response)) != (long)sizeof(response)
            || response < 0x10000 || response > UINT64_MAX - 0x18
            || agy_safe_read(response + 8, text, sizeof(text)) != (long)sizeof(text))
        return;
    cgt_response_packet("app_response_packet", b->regs.rax, text[0], text[1]);
}

static void cgt_bytes_emit(const char *kind, uint64_t id, uint64_t ptr, uint64_t len)
{
    if (ptr < 0x10000 || len == 0 || len > (16u << 20)) return;
    char buf[16384];
    for (uint64_t off = 0; off < len; off += sizeof(buf)) {
        size_t n = (size_t)std::min<uint64_t>(len - off, sizeof(buf));
        if (agy_safe_read(ptr + off, buf, n) != (long)n) return;
        wire_event_t ev = {.kind = kind, .stream_id = id,
                          .data = (const uint8_t *)buf, .len = n, .mode = WIRE_ASYNC};
        wire_emit(&ev);
    }
}

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
        agy_emit_stack(book_id(b), b->regs.rbp, g_gh_base);
}

void agy_on_generic(agy_block *b)
{
    cgt_common(b);
    emit_book(b, b->regs.rax);
}

void agy_on_fh_update(agy_block *b)
{
    cgt_common(b);
    cgt_response_emit(b);
    emit_book(b, b->regs.rax);
}

void agy_on_stream(agy_block *b)
{
    cgt_common(b);
    cgt_bytes_emit(book_id(b), b->regs.rax, b->regs.rbx, b->regs.rcx);
}

void agy_on_file_open(agy_block *b)
{
    cgt_common(b);
    const char *p = (const char *)b->regs.rax;
    size_t len = (size_t)b->regs.rbx;
    if (p && len > 0 && len < 4096 &&
        (mem_has(p, len, "conversations/") || mem_has(p, len, "/brain/"))) {
        wire_event_t ev = { .kind = book_id(b), .stream_id = 0,
                           .data = (const uint8_t *)p, .len = len, .mode = WIRE_ASYNC };
        wire_emit(&ev);
    }
}

void agy_on_http_rt(agy_block *b)
{
    cgt_common(b);
    emit_book(b, b->regs.rbx);
}

void agy_on_exit(agy_block *b)
{
    cgt_common(b);
    emit_book(b, b->regs.rax, WIRE_SYNC);
    wire_shutdown();
}

void agy_on_resp_chunk(agy_block *b)
{
    cgt_common(b);
    cgt_response_packet("resp_chunk_packet", b->regs.rax, b->regs.rax, b->regs.rbx);
}

void agy_on_system_message(agy_block *b)
{
    cgt_common(b);
    /* Pinned Go layouts, checked against GetType/GetSystemMessage and the two serializers:
     * DefaultSerializer's value receiver is stack-passed, so Step is RAX; the old converter
     * takes receiver, context (two words), Step, making Step RDI. Step+0x60 is the oneof
     * wrapper, whose first word is *CortexStepSystemMessage. No Go pointers escape this call. */
    const uint64_t step = b->book == (uint32_t)bk("system_message") ? b->regs.rax : b->regs.rdi;
    uint32_t type = 0;
    uint64_t wrapper = 0, message = 0;
    if (step < 0x10000 || step > UINT64_MAX - 0x68
            || agy_safe_read(step + 8, &type, sizeof(type)) != (long)sizeof(type) || type != 101
            || agy_safe_read(step + 0x60, &wrapper, sizeof(wrapper)) != (long)sizeof(wrapper)
            || agy_safe_read(wrapper, &message, sizeof(message)) != (long)sizeof(message)
            || message < 0x10000 || message > UINT64_MAX - 0x30)
        return;
    uint64_t fields[6];
    if (agy_safe_read(message, fields, sizeof(fields)) != (long)sizeof(fields)) return;
    const uint64_t text_ptr = fields[1], text_len = fields[2];
    const uint64_t event_ptr = fields[4], event_len = fields[5];
    if (text_len > INT64_MAX || event_len > INT64_MAX) return;

    /* One event holds both strings, so concurrent Go producers cannot interleave chunks.
     * Stay within the bridge's default 1 MiB copy limit. Original lengths make truncation
     * explicit; the Python decoder also notices a smaller WIRE_MAXCOPY cutting this packet. */
    struct header {
        char magic[4];
        uint32_t version;
        uint64_t text_len, event_len;
        uint32_t text_copied, event_copied;
    } h = {{'A', 'G', 'S', 'M'}, 1, text_len, event_len, 0, 0};
    static_assert(sizeof(header) == 32);
    constexpr size_t limit = 1u << 20;
    h.event_copied = (uint32_t)std::min<uint64_t>(event_len, 4096);
    h.text_copied = (uint32_t)std::min<uint64_t>(text_len, limit - sizeof(h) - h.event_copied);
    const size_t size = sizeof(h) + h.event_copied + h.text_copied;
    std::unique_ptr<uint8_t[]> packet(new (std::nothrow) uint8_t[size]);
    if (!packet) return;
    std::memcpy(packet.get(), &h, sizeof(h));
    if ((h.event_copied && agy_safe_read(event_ptr, packet.get() + sizeof(h), h.event_copied)
                           != h.event_copied)
            || (h.text_copied && agy_safe_read(text_ptr, packet.get() + sizeof(h) + h.event_copied,
                                             h.text_copied) != h.text_copied))
        return;
    wire_event_t ev = {.kind = book_id(b), .stream_id = step, .data = packet.get(),
                       .len = size, .mode = WIRE_ASYNC};
    wire_emit(&ev);
}

void agy_on_readlink_filter(agy_block *b)
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
        static std::atomic_flag logged = ATOMIC_FLAG_INIT;
        if (!logged.test_and_set(std::memory_order_relaxed)) {
            char kr[512];
            ssize_t n = readlink("/proc/self/exe", kr, sizeof kr - 1);
            kr[n > 0 ? n : 0] = 0;
            GHLOG("os.readlink(/proc/self/exe): kernel=%s -> RETURN %s", kr, g_real_exe);
        }
        wire_event_t ev = { .kind = book_id(b), .stream_id = nptr,
                           .data = (const uint8_t *)g_real_exe, .len = g_real_exe_len,
                           .mode = WIRE_ASYNC };
        wire_emit(&ev);
    }
}

void agy_enable_books(uint64_t base, bool conv_id)
{
    g_gh_base   = base;
    g_cgt_args  = std::getenv("AGY_PROC_CGT_ARGS") != nullptr;
    g_cgt_stack = std::getenv("AGY_PROC_STACK") != nullptr;

    int n_tramp = 0, n_asm = 0;
    for (int i = 0; i < BK_COUNT; i++) {
        const auto &h = BOOKS[i];
        if (h.mech == AGY_OFF || (i == bk("file_open") && !conv_id)) {
            g_agy_on[i] = nullptr;
            continue;
        }
        g_agy_on[i] = agy_handler_for(h.id);
        n_tramp++;
        n_asm += (h.mech == AGY_ASMGO);
        GHLOG("cgo-trampoline %.*s", (int)h.id.size(), h.id.data());
    }
    wire_logline(STDERR_FILENO, "[antigravity] cgocall-trampoline: installed %d/%d row(s) (%d asmcgo, %d full-cgo)",
                 n_tramp, BK_COUNT, n_asm, n_tramp - n_asm);
}
