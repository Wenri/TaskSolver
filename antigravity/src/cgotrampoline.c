/* cgotrampoline.c — cgocall-trampoline hooks for parking Go functions.
 *
 * A gum Interceptor rewrites the return address and tracks it per-OS-thread, so
 * hooking a function that PARKS the goroutine (it resumes on another M) corrupts
 * gum's bookkeeping (this is what stalled agy at stages 5/6). Instead we redirect
 * the target — past its stack-check prologue — to a generated trampoline that:
 *   1. snapshots the Go-ABI arg registers into a stack block,
 *   2. CALLs runtime.cgocall(fn=agy_cgo_hook, arg=&block), which switches to the
 *      g0 stack and runs our C hook in a safe cgo context (arg arrives in RDI),
 *   3. restores the registers, runs the overwritten original instructions, and
 *      jmps back into the target body.
 * The trampoline touches no return address of ours, so parking/rescheduling is
 * unaffected. cgocall's entersyscall() opens a GC-scannable window over the
 * trampoline frame; agy_gomod_register() covers it with a synthetic moduledata
 * so findfunc resolves the trampoline PCs (else throw("unknown pc")). See gomod.h.
 */
#define _GNU_SOURCE
#include "frida-gum.h"
#include "gomod.h"
#include "cgotrampoline.h"
#include "pybridge.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/uio.h>

#define GHLOG(...) do { fprintf(stderr, "[antigravity/gohook] " __VA_ARGS__); \
                        fputc('\n', stderr); fflush(stderr); } while (0)

/* Go enters a function with rsp ≡ 8 (mod 16). The `call` inside the trampoline
 * needs rsp ≡ 0 (mod 16) (SysV for the C hook; Go/cgocall likewise), so the frame
 * we subtract must be ≡ 8 (mod 16).
 *
 * The bottom GH_SPILL bytes are DEAD outgoing-call scratch. runtime.cgocall uses Go's
 * internal ABI and spills its two register args (fn, arg) into CALLER-provided slots at
 * [rsp] and [rsp+8] at the `call` — so the block must NOT sit there, or cgocall clobbers
 * block.kind (@[rsp]) and block.regs.rax (@[rsp+8]) before our hook reads them. We reserve
 * those 16 bytes BELOW the block by baking them into the fixed frame — NOT a transient
 * `sub` around the call: cgocall enters _Gsyscall, during which GC unwinds our frame via
 * the synthetic pcsp, which must report ONE constant spdelta across the whole
 * [frame_lo,frame_hi) window. A transient sub would make the real spdelta frame+N there
 * while pcsp says frame → throw("unknown pc"). (The asmcgo path may use a transient sub:
 * it never enters _Gsyscall, so GC never scans our frame mid-call.)
 *   GH_FRAME = GH_SPILL(16) + block(96) + 8 pad = 120  (120 ≡ 8 mod 16 ✓). */
#define GH_SPILL  16          /* cgocall's caller-provided register-arg spill slots */
#define GH_FRAME  (GH_SPILL + 96 + 8)   /* == 120 */
#define GH_SLOT   256         /* per-trampoline slot (== FuncTabBucketSize/16) */

/* block offsets (rsp-relative after `sub rsp,GH_FRAME`); mirror agy_block. The block
 * sits ABOVE the GH_SPILL scratch, so every offset is GH_SPILL-based. */
enum { OFF_KIND = GH_SPILL,      OFF_RAX = GH_SPILL + 8,  OFF_RBX = GH_SPILL + 16,
       OFF_RCX  = GH_SPILL + 24, OFF_RDI = GH_SPILL + 32, OFF_RSI = GH_SPILL + 40,
       OFF_R8   = GH_SPILL + 48, OFF_R9  = GH_SPILL + 56, OFF_R10 = GH_SPILL + 64,
       OFF_R11  = GH_SPILL + 72, OFF_RDX = GH_SPILL + 80, OFF_RBP = GH_SPILL + 88 };

/* xorps xmm15,xmm15 — Go's ABI requires X15 zeroed across a (asm)cgocall
 * boundary; emitted verbatim into both call paths below. */
static const guint8 XORPS_XMM15[] = { 0x45, 0x0f, 0x57, 0xff };

/* Fault-safe read of up to n bytes from a possibly-bogus address in our OWN
 * process: process_vm_readv returns -1/EFAULT for unmapped pages instead of
 * segfaulting, so we can probe unknown arg registers without knowing the Go
 * signature. A plain syscall — safe on the g0 stack (no Go allocation). */
long agy_safe_read(uint64_t addr, void *dst, unsigned long n)
{
    if (addr < 0x1000) return -1;
    struct iovec local = { dst, n };
    struct iovec remote = { (void *)(uintptr_t)addr, n };
    return process_vm_readv(getpid(), &local, 1, &remote, 1, 0);
}

/* Walk the Go frame-pointer chain: [rbp]=saved caller rbp, [rbp+8]=return addr.
 * Go keeps frame pointers (framepointer_enabled). Stores up to `max` return PCs
 * (reduced to link vaddrs, pc-base). Fault-safe; terminates on rbp==0 / a
 * non-monotonic (must strictly increase up the stack) / unreadable frame. */
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

/* Emit a "callstack" event (gated by AGY_PROC_STACK at the call sites): the source
 * hook kind (NUL-terminated) followed by the packed u64 frame vaddrs.
 *
 * Hot hooks (crypto/tls decrypt runs per TLS record) repeat the *same* few call
 * stacks thousands of times; emitting each would flood the worker queue and
 * backpressure the TLS goroutine — which stalls the turn. So we DEDUP: each
 * distinct (kind, stack) is emitted exactly once. We still walk per fire (cheap,
 * fault-safe), but skip the expensive copy+enqueue for repeats. */
void agy_emit_stack(const char *src_kind, uint64_t rbp, uint64_t base)
{
    uint64_t frames[48];
    int n = agy_backtrace(rbp, base, frames, 48);
    if (n <= 0) return;

    uint64_t h = 1469598103934665603ULL ^ (uint64_t)(uintptr_t)src_kind;
    for (int i = 0; i < n; i++) { h ^= frames[i]; h *= 1099511628211ULL; }
    static uint64_t seen[1024];
    static int nseen;
    for (int i = 0; i < nseen; i++) if (seen[i] == h) return;   /* already emitted */
    if (nseen < 1024) seen[nseen++] = h; else return;           /* set full → stop */

    unsigned char buf[64 + 48 * 8];
    size_t kl = strlen(src_kind);
    if (kl > 48) kl = 48;
    memcpy(buf, src_kind, kl);
    buf[kl] = 0;
    size_t off = kl + 1;
    memcpy(buf + off, frames, (size_t)n * 8);
    off += (size_t)n * 8;
    agy_event_t ev = { .kind = "callstack", .stream_id = rbp,
                       .data = buf, .len = off, .mode = AGY_ASYNC };
    agy_py_emit(&ev);
}

/* Diagnostic (AGY_PROC_CGT_ARGS=1): build a human-readable report of the arg
 * registers + fault-safe memory samples so we can reverse-engineer which register
 * holds the message / response-delta (agy is stripped → signatures unknown). ALL
 * dereferencing happens here, in-process — the JSONL is read after agy exits. */
static void cgt_diag_append_string(char *rep, size_t cap, size_t *o,
                                    const char *label, uint64_t ptr, uint64_t len)
{
    if (*o >= cap || len == 0 || len > 4096) return;
    unsigned char tmp[256];
    size_t n = len < sizeof(tmp) ? (size_t)len : sizeof(tmp);
    if (agy_safe_read(ptr, tmp, n) != (ssize_t)n) return;
    /* only report if it looks like text (mostly printable) */
    size_t printable = 0;
    for (size_t i = 0; i < n; i++)
        if (tmp[i] == '\t' || tmp[i] == '\n' || (tmp[i] >= 0x20 && tmp[i] < 0x7f)) printable++;
    if (printable * 10 < n * 8) return;                     /* <80% printable → skip */
    *o += snprintf(rep + *o, cap - *o, "  %s(len=%llu)=\"", label,
                   (unsigned long long)len);
    for (size_t i = 0; i < n && *o < cap - 2; i++)
        rep[(*o)++] = (tmp[i] >= 0x20 && tmp[i] < 0x7f) ? (char)tmp[i] :
                      (tmp[i] == '\n' ? ' ' : '.');
    if (*o < cap - 2) rep[(*o)++] = '"';
    if (*o < cap - 2) rep[(*o)++] = '\n';
    rep[*o] = 0;
}

/* Recursively walk the Go object graph from an arg register, reporting any
 * printable (ptr,len) Go-string header found, with its access path. Bounded by a
 * read budget + a visited set (cycle guard); depth-limited. */
struct walkctx { char *rep; size_t cap; size_t *o; int budget; uint64_t seen[1024]; int nseen; };

static int cgt_seen(struct walkctx *c, uint64_t a)
{
    for (int i = 0; i < c->nseen; i++) if (c->seen[i] == a) return 1;
    if (c->nseen < 1024) c->seen[c->nseen++] = a;
    return 0;
}

static void cgt_walk(struct walkctx *c, uint64_t addr, int depth, const char *path)
{
    if (c->budget <= 0 || addr < 0x10000 || *c->o > c->cap - 400) return;
    if (cgt_seen(c, addr)) return;
    unsigned char buf[64];
    c->budget--;
    if (agy_safe_read(addr, buf, sizeof(buf)) != (ssize_t)sizeof(buf)) return;
    /* adjacent (ptr,len) word pairs → candidate Go strings */
    for (int w = 0; w + 1 < 8; w++) {
        uint64_t p, l;
        memcpy(&p, buf + w * 8, 8);
        memcpy(&l, buf + (w + 1) * 8, 8);
        char lbl[72];
        snprintf(lbl, sizeof(lbl), "%s+%d", path, w * 8);
        cgt_diag_append_string(c->rep, c->cap, c->o, lbl, p, l);
    }
    if (depth <= 0) return;
    for (int w = 0; w < 8; w++) {
        uint64_t p;
        memcpy(&p, buf + w * 8, 8);
        if (p > 0x10000 && p != addr) {
            char np[72];
            snprintf(np, sizeof(np), "%s+%d", path, w * 8);
            cgt_walk(c, p, depth - 1, np);
        }
    }
}

static void cgt_diag(agy_block *b)
{
    char rep[16384];
    size_t o = 0;
    const char *ds = getenv("AGY_PROC_CGT_DEPTH");
    const char *bs = getenv("AGY_PROC_CGT_BUDGET");
    int depth = ds ? atoi(ds) : 3;
    int budget = bs ? atoi(bs) : 220;
    uint64_t r[10] = { b->regs.rax, b->regs.rbx, b->regs.rcx, b->regs.rdi, b->regs.rsi,
                       b->regs.r8, b->regs.r9, b->regs.r10, b->regs.r11, b->regs.rdx };
    static const char *nm[10] = { "rax", "rbx", "rcx", "rdi", "rsi",
                                  "r8", "r9", "r10", "r11", "rdx" };
    o += snprintf(rep + o, sizeof(rep) - o, "kind=%s recv=0x%llx\n",
                  (const char *)b->kind, (unsigned long long)r[0]);
    /* per-register value + 24-byte pointee ascii preview */
    for (int i = 0; i < 10 && o < sizeof(rep) - 80; i++) {
        unsigned char s[24];
        char asc[25];
        int got = agy_safe_read(r[i], s, sizeof(s)) == (ssize_t)sizeof(s);
        if (got) {
            for (size_t k = 0; k < sizeof(s); k++)
                asc[k] = (s[k] >= 0x20 && s[k] < 0x7f) ? (char)s[k] : '.';
            asc[sizeof(s)] = 0;
        }
        o += snprintf(rep + o, sizeof(rep) - o, " %s=0x%llx%s%s\n", nm[i],
                      (unsigned long long)r[i], got ? " ~" : "", got ? asc : "");
    }
    /* immediate (ptr,len) arg-pair strings, then a bounded object-graph walk from
     * each likely arg container (rbx/rcx/rdi/rsi) to surface nested text. */
    for (int i = 1; i < 9; i++)
        cgt_diag_append_string(rep, sizeof(rep), &o, nm[i], r[i], r[i + 1]);
    struct walkctx c = { rep, sizeof(rep), &o, budget, {0}, 0 };
    for (int i = 0; i <= 4; i++)                 /* rax(receiver), rbx, rcx, rdi, rsi */
        cgt_walk(&c, r[i], depth, nm[i]);
    agy_event_t ev = { .kind = "cgt_args", .stream_id = r[0],
                       .data = (const uint8_t *)rep, .len = o, .mode = AGY_ASYNC };
    agy_py_emit(&ev);
}

/* Plan 7 — clean RESPONSE decode at the SHALLOW consumer boundary. The live probe
 * (AGY_PROC_CGT_ARGS at stage 12) settled which framework function carries the
 * assembled assistant text nearest the surface: NOT AppendStep/OnStepsChanged (both
 * 6 struct-hops deep, AgentState-internal), but
 * generator.(*streamResponseHandler).updateWithStep — its RSI arg points to the
 * planner response whose text is a Go string at +0x8(ptr)/+0x10(len), ONE deref, the
 * stable cortex proto layout (thinking sits deeper at +0x28). updateWithStep fires a
 * few times/turn; the text-bearing fires carry the FULL answer (not per-delta
 * fragments), the others have an empty response string → skipped by the len check.
 * Fault-safe (agy_safe_read) + read into a local buffer (agy_py_emit copies it). */
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
    size_t printable = 0;                              /* reject non-text (wrong field) */
    for (size_t i = 0; i < n; i++)
        if (buf[i] == '\t' || buf[i] == '\n' || ((unsigned char)buf[i] >= 0x20 &&
            (unsigned char)buf[i] < 0x7f)) printable++;
    if (printable * 10 < n * 8) return;                /* <80% printable → not the text */
    agy_event_t ev = { .kind = "app_response", .stream_id = b->regs.rax,
                       .data = (const uint8_t *)buf, .len = n, .mode = AGY_ASYNC };
    agy_py_emit(&ev);
}

/* Emit an entry-arg []byte (ptr/len already sitting in arg registers) as a capture
 * event — the trampoline analog of the gum on_enter []byte path. We can't deref a Go
 * pointer directly on the g0 stack, so safe-read a bounded copy (truncates at
 * CGT_RESP_CAP; for chunked response bodies the reassembler stitches successive fires). */
static void cgt_bytes_emit(const char *kind, uint64_t id, uint64_t ptr, uint64_t len)
{
    if (ptr < 0x10000 || len == 0 || len > (16u << 20)) return;
    char buf[CGT_RESP_CAP];
    size_t n = len < CGT_RESP_CAP ? (size_t)len : CGT_RESP_CAP;
    if (agy_safe_read(ptr, buf, n) != (long)n) return;
    agy_event_t ev = { .kind = kind, .stream_id = id,
                       .data = (const uint8_t *)buf, .len = n, .mode = AGY_ASYNC };
    agy_py_emit(&ev);
}

/* The C hook — runs on the g0/system stack during cgocall. MUST stay light and
 * must not allocate Go memory; agy_py_emit() copies + enqueues to the worker.
 * Emits the receiver (RAX) as stream_id + the borrowed rodata kind tag. With
 * AGY_PROC_CGT_ARGS set, also emits a diagnostic arg-register report. */
static uint64_t g_gh_base;   /* main-module base, for reducing PCs to link vaddrs */

static void agy_cgo_hook(agy_block *b)
{
    if (getenv("AGY_PROC_CGT_ARGS")) cgt_diag(b);
    if (getenv("AGY_PROC_STACK"))    /* captured rbp = the CALLER's frame (target
                                        prologue runs on the way out) → chain starts
                                        one frame above the target (= the kind). */
        agy_emit_stack((const char *)b->kind, b->regs.rbp, g_gh_base);
    /* updateWithStep is the shallow response consumer → emit the clean answer text
     * (in addition to the fire-count event below, which other kinds also emit). */
    if (b->kind && strcmp((const char *)b->kind, "fh_update") == 0)
        cgt_response_emit(b);
    /* Entry-arg hooks migrated off gum because they PARK (the trampoline is park-safe).
     * These emit their own full event and return — no generic fire event below. */
    if (b->kind && strcmp((const char *)b->kind, "resp") == 0) {
        /* http2 (*pipe).Write(p []byte): receiver=rax, p.ptr=rbx, p.len=rcx */
        cgt_bytes_emit("resp", b->regs.rax, b->regs.rbx, b->regs.rcx);
        return;
    }
    if (b->kind && strcmp((const char *)b->kind, "tls_write") == 0) {
        /* crypto/tls.(*Conn).Write(c=rax, b.ptr=rbx, b.len=rcx): the model REQUEST (egress).
         * Entry-arg read on the trampoline — the reliable replacement for the gum on_enter path. */
        cgt_bytes_emit("tls_write", b->regs.rax, b->regs.rbx, b->regs.rcx);
        return;
    }
    if (b->kind && strcmp((const char *)b->kind, "http_rt") == 0) {
        /* net/http.(*Transport).RoundTrip(t=rax, req=rbx): marker keyed by the request ptr */
        agy_event_t rt = { .kind = "http_rt", .stream_id = b->regs.rbx, .mode = AGY_ASYNC };
        agy_py_emit(&rt);
        return;
    }
    if (b->kind && strcmp((const char *)b->kind, "exit") == 0) {
        /* os.Exit(code int): code=rax. The clean end-of-capture marker. SYNC so the worker
         * writes it BEFORE agy's exit_group syscall (an ASYNC event would race process death);
         * FULLCGO hands off the P so other goroutines run while we briefly block. The code rides
         * stream_id; Python on_exit records {"kind":"exit","code":N}. */
        agy_event_t ev = { .kind = "exit", .stream_id = b->regs.rax, .mode = AGY_SYNC };
        agy_py_emit(&ev);        /* SYNC: marker recorded before we stop the worker (below) */
        agy_py_free(&ev);
        agy_py_shutdown();       /* now cooperatively stop + join the worker (deterministic teardown) */
        return;
    }
    agy_event_t ev = { .kind = (const char *)b->kind,
                       .stream_id = b->regs.rax, .mode = AGY_ASYNC };
    agy_py_emit(&ev);
}

/* Recognize the Go frame-setup prologue we overwrite: push rbp; mov rbp,rsp;
 * sub rsp,imm. Returns its byte length (>= 5, a whole number of position-
 * independent instructions), or 0 if the layout is unexpected (refuse). */
static uint32_t match_prologue(const uint8_t *p)
{
    if (p[0] != 0x55) return 0;                                  /* push rbp */
    if (!(p[1] == 0x48 && p[2] == 0x89 && p[3] == 0xe5)) return 0; /* mov rbp,rsp */
    if (p[4] == 0x48 && p[5] == 0x81 && p[6] == 0xec) return 1 + 3 + 7; /* sub rsp,imm32 */
    if (p[4] == 0x48 && p[5] == 0x83 && p[6] == 0xec) return 1 + 3 + 4; /* sub rsp,imm8  */
    return 0;
}

struct patch_ctx { uint8_t bytes[16]; gsize n; };
static void patch_apply(gpointer mem, gpointer ud)
{
    struct patch_ctx *c = ud;
    memcpy(mem, c->bytes, c->n);
}

struct agy_gohook {
    uint64_t base, cgocall_abs, asmcgocall_abs;
    guint8 *region;
    GumX86Writer *w;
    int made, max;
    uint32_t frame_lo, frame_hi;
    int frame_is_fullcgo;   /* the shared pcsp must match a full-cgo slot (only those get GC-unwound) */
};

agy_gohook *agy_gohook_begin(uint64_t base, uint64_t cgocall_va, uint64_t asmcgocall_va,
                             int max_targets)
{
    if (!cgocall_va)      { GHLOG("runtime.cgocall unresolved; cannot build trampolines"); return NULL; }
    if (!asmcgocall_va)   { GHLOG("runtime.asmcgocall unresolved; cannot build trampolines"); return NULL; }
    if (max_targets <= 0) { GHLOG("bad max_targets=%d", max_targets); return NULL; }

    agy_gohook *h = calloc(1, sizeof *h);
    if (!h) { GHLOG("calloc failed"); return NULL; }
    h->base = base;
    h->cgocall_abs = base + cgocall_va;
    h->asmcgocall_abs = base + asmcgocall_va;   /* required, assumed resolved (checked above) */
    h->max = max_targets;
    g_gh_base = base;                           /* for agy_emit_stack PC→vaddr reduction */

    /* Slot region NEAR agy's text so each target->trampoline jmp is a 5-byte rel32. */
    long page = sysconf(_SC_PAGESIZE);
    gsize need = (gsize)max_targets * GH_SLOT;
    guint npages = (guint)((need + page - 1) / page);
    GumAddressSpec spec = { (gpointer)(uintptr_t)base, 0x7f000000 };  /* within +-~2GB of text */
    h->region = gum_alloc_n_pages_near(npages, GUM_PAGE_RWX, &spec);
    if (!h->region) { GHLOG("gum_alloc_n_pages_near failed"); free(h); return NULL; }
    h->w = gum_x86_writer_new(h->region);
    return h;
}

void agy_gohook_add(agy_gohook *h, uint64_t entry, uint32_t skip, const char *kind, int asmcgo)
{
    if (!h) return;
    if (h->made >= h->max) { GHLOG("region full (max=%d); dropping kind=%s", h->max, kind); return; }

    uint64_t hook_addr = h->base + entry + skip;
    const uint8_t *orig = (const uint8_t *)(uintptr_t)hook_addr;
    uint32_t ov = match_prologue(orig);
    if (!ov) { GHLOG("unexpected prologue at +%u (kind=%s); skipping", skip, kind); return; }

    GumX86Writer *w = h->w;
    guint8 *slot = h->region + (size_t)h->made * GH_SLOT;
    gum_x86_writer_reset(w, slot);
    uint32_t lo = 0, hi = 0;

    /* prologue: reserve frame, snapshot the arg registers into the block */
    gum_x86_writer_put_sub_reg_imm(w, GUM_X86_RSP, GH_FRAME);
    lo = gum_x86_writer_offset(w);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_RAX, GUM_X86_RAX);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_RBX, GUM_X86_RBX);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_RCX, GUM_X86_RCX);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_RDI, GUM_X86_RDI);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_RSI, GUM_X86_RSI);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_R8,  GUM_X86_R8);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_R9,  GUM_X86_R9);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_R10, GUM_X86_R10);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_R11, GUM_X86_R11);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_RDX, GUM_X86_RDX);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_RBP, GUM_X86_RBP);
    /* block.kind = borrowed const char* (imm64) via RAX (already saved) */
    gum_x86_writer_put_mov_reg_address(w, GUM_X86_RAX, (GumAddress)(uintptr_t)kind);
    gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_KIND, GUM_X86_RAX);

    /* Register the synthetic moduledata (once) BEFORE the cgocall opens the
     * _Gsyscall GC-scan window. Runs on the goroutine stack in _Grunning (our
     * unknown PC is never an async-safe-point → no scan/preempt), and only
     * clobbers already-saved caller-saved regs. rsp is 16-aligned here. */
    gum_x86_writer_put_mov_reg_address(w, GUM_X86_RAX, (GumAddress)(uintptr_t)agy_gomod_ensure);
    gum_x86_writer_put_call_reg(w, GUM_X86_RAX);

    if (asmcgo) {
        /* asmcgocall(fn, arg) — the g0-stack-switch inner half of cgocall, WITHOUT
         * entersyscall/exitsyscall (no _Gsyscall, no P handoff, no reschedule-onto-
         * new-stack). asmcgocall takes ABI0 STACK args (verified in agy): the caller
         * places fn@[sp+0], arg@[sp+8], errno@[sp+16] at the CALL, and it re-derives
         * g from TLS + passes arg to fn in RDI. We carve a transient 32-byte outgoing
         * frame BELOW the block (kept 16-aligned) so the block the hook reads stays
         * intact. NOTE: within [frame_lo,frame_hi) the moduledata claims spdelta==FRAME,
         * but here it's FRAME+32 — benign: asmcgocall never enters _Gsyscall, so GC does
         * no non-preemptive stack scan of this window (the cgocall hazard). */
        gum_x86_writer_put_lea_reg_reg_offset(w, GUM_X86_RSI, GUM_X86_RSP, OFF_KIND); /* rsi=&block */
        gum_x86_writer_put_sub_reg_imm(w, GUM_X86_RSP, 32);
        gum_x86_writer_put_mov_reg_address(w, GUM_X86_RAX, (GumAddress)(uintptr_t)agy_cgo_hook);
        gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, 0, GUM_X86_RAX);   /* [sp+0]=fn  */
        gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, 8, GUM_X86_RSI);   /* [sp+8]=arg */
        gum_x86_writer_put_bytes(w, XORPS_XMM15, sizeof XORPS_XMM15);
        gum_x86_writer_put_mov_reg_address(w, GUM_X86_R12, (GumAddress)h->asmcgocall_abs);
        gum_x86_writer_put_call_reg(w, GUM_X86_R12);
        gum_x86_writer_put_add_reg_imm(w, GUM_X86_RSP, 32);
    } else {
        /* cgocall(fn=RAX, arg=RBX): arg=&block, fn=&agy_cgo_hook; X15 must be 0.
         * The 16-byte GH_SPILL scratch below the block absorbs cgocall's two
         * caller-arg spill slots ([S],[S+8]), so block.kind/regs.rax stay intact. */
        gum_x86_writer_put_lea_reg_reg_offset(w, GUM_X86_RBX, GUM_X86_RSP, OFF_KIND);
        gum_x86_writer_put_mov_reg_address(w, GUM_X86_RAX, (GumAddress)(uintptr_t)agy_cgo_hook);
        gum_x86_writer_put_bytes(w, XORPS_XMM15, sizeof XORPS_XMM15);
        gum_x86_writer_put_mov_reg_address(w, GUM_X86_R12, (GumAddress)h->cgocall_abs);
        gum_x86_writer_put_call_reg(w, GUM_X86_R12);
    }

    /* restore the target's registers, then close the frame */
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_RAX, GUM_X86_RSP, OFF_RAX);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_RBX, GUM_X86_RSP, OFF_RBX);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_RCX, GUM_X86_RSP, OFF_RCX);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_RDI, GUM_X86_RSP, OFF_RDI);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_RSI, GUM_X86_RSP, OFF_RSI);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_R8,  GUM_X86_RSP, OFF_R8);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_R9,  GUM_X86_RSP, OFF_R9);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_R10, GUM_X86_RSP, OFF_R10);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_R11, GUM_X86_RSP, OFF_R11);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_RDX, GUM_X86_RSP, OFF_RDX);
    gum_x86_writer_put_mov_reg_reg_offset_ptr(w, GUM_X86_RBP, GUM_X86_RSP, OFF_RBP);
    hi = gum_x86_writer_offset(w);
    gum_x86_writer_put_add_reg_imm(w, GUM_X86_RSP, GH_FRAME);

    /* run the overwritten original instructions, then jmp back past them */
    gum_x86_writer_put_bytes(w, orig, ov);
    gum_x86_writer_put_jmp_address(w, (GumAddress)(hook_addr + ov));
    gum_x86_writer_flush(w);
    /* The shared pcsp must describe a FULL-CGO slot's frame window — those are the only
     * slots GC unwinds (asmcgo never enters _Gsyscall, so its frames are never scanned).
     * Prefer a full-cgo slot; fall back to an asmcgo one only if the region is all-asmcgo. */
    if (!asmcgo) { h->frame_lo = lo; h->frame_hi = hi; h->frame_is_fullcgo = 1; }
    else if (!h->frame_is_fullcgo) { h->frame_lo = lo; h->frame_hi = hi; }

    /* patch target+skip: jmp rel32 to the slot, nop-pad to the whole prologue */
    struct patch_ctx pc = { .n = ov };
    memset(pc.bytes, 0x90, ov);
    int32_t rel = (int32_t)((int64_t)(uintptr_t)slot - (int64_t)(hook_addr + 5));
    pc.bytes[0] = 0xe9;
    memcpy(pc.bytes + 1, &rel, 4);
    if (!gum_memory_patch_code((gpointer)(uintptr_t)hook_addr, ov, patch_apply, &pc)) {
        /* target prologue wasn't overwritten → the trampoline is unreachable. Don't count
         * it (the moduledata covers only `made` slots) and reuse this slot on the next add. */
        GHLOG("patch failed for kind=%s @ +%u — trampoline NOT installed, skipping", kind, skip);
        return;
    }

    GHLOG("cgo-trampoline kind=%s @ +%u -> %p (overwrite %u bytes)", kind, skip, (void *)slot, ov);
    h->made++;
}

int agy_gohook_finalize(agy_gohook *h, uint64_t md_vaddr)
{
    if (!h) return 0;
    if (h->w) gum_x86_writer_unref(h->w);
    int made = h->made;
    if (made == 0) { GHLOG("no trampolines installed"); free(h); return 0; }

    /* GC-unwind safety: BUILD the covering synthetic moduledata now (constructor
     * time; no firstmoduledata read). The trampolines call agy_gomod_ensure() to
     * SPLICE it lazily on first hit, when Go is up. Required — without it a GC that
     * unwinds a trampoline frame throws("unknown pc"). */
    uintptr_t rbase = (uintptr_t)h->region;
    int rc = agy_gomod_prepare(h->base + md_vaddr, h->cgocall_abs,
                               rbase, rbase + (uintptr_t)made * GH_SLOT,
                               GH_SLOT, made, GH_FRAME, h->frame_lo, h->frame_hi);
    if (rc != 0) GHLOG("moduledata prepare failed (rc=%d); trampolines are live "
                       "but NOT GC-safe — expect throw(\"unknown pc\") under GC", rc);
    free(h);
    return made;
}
