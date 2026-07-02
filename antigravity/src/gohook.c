/* gohook.c — cgocall-trampoline hooks for parking Go functions.
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
#include "pybridge.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>

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

/* The C hook — runs on the g0/system stack during cgocall. MUST stay light and
 * must not allocate Go memory; agy_py_emit() copies + enqueues to the worker.
 * Emits the receiver (RAX) as stream_id + the borrowed rodata kind tag; decoded
 * payload extraction lands next. */
static void agy_cgo_hook(agy_block *b)
{
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

int agy_gohook_install(uint64_t base, uint64_t cgocall_va, uint64_t asmcgocall_va,
                       uint64_t md_vaddr, const agy_gh_target *targets, int n)
{
    if (!cgocall_va) { GHLOG("runtime.cgocall unresolved; cannot build trampolines"); return 0; }
    uint64_t cgocall_abs = base + cgocall_va;
    uint64_t asmcgocall_abs = asmcgocall_va ? base + asmcgocall_va : 0;
    int asmcgo_on = getenv("AGY_PROC_ASMCGO") != NULL;
    if (asmcgo_on && !asmcgocall_abs) {
        GHLOG("AGY_PROC_ASMCGO set but runtime.asmcgocall unresolved; falling back to cgocall");
        asmcgo_on = 0;
    }

    /* Slot region NEAR agy's text so the target->trampoline jmp is a 5-byte rel32. */
    long page = sysconf(_SC_PAGESIZE);
    gsize need = (gsize)n * GH_SLOT;
    guint npages = (guint)((need + page - 1) / page);
    GumAddressSpec spec = { (gpointer)(uintptr_t)base, 0x7f000000 };  /* within +-~2GB of text */
    guint8 *region = gum_alloc_n_pages_near(npages, GUM_PAGE_RWX, &spec);
    if (!region) { GHLOG("gum_alloc_n_pages_near failed"); return 0; }

    GumX86Writer *w = gum_x86_writer_new(region);
    uint32_t frame_lo = 0, frame_hi = 0;
    int made = 0;

    for (int i = 0; i < n; i++) {
        uint64_t hook_addr = base + targets[i].entry + targets[i].skip;
        const uint8_t *orig = (const uint8_t *)(uintptr_t)hook_addr;
        uint32_t ov = match_prologue(orig);
        if (!ov) { GHLOG("unexpected prologue at +%u (kind=%s); skipping",
                         targets[i].skip, targets[i].kind); continue; }

        guint8 *slot = region + (size_t)made * GH_SLOT;
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
        gum_x86_writer_put_mov_reg_address(w, GUM_X86_RAX, (GumAddress)(uintptr_t)targets[i].kind);
        gum_x86_writer_put_mov_reg_offset_ptr_reg(w, GUM_X86_RSP, OFF_KIND, GUM_X86_RAX);

        /* Register the synthetic moduledata (once) BEFORE the cgocall opens the
         * _Gsyscall GC-scan window. Runs on the goroutine stack in _Grunning (our
         * unknown PC is never an async-safe-point → no scan/preempt), and only
         * clobbers already-saved caller-saved regs. rsp is 16-aligned here. */
        gum_x86_writer_put_mov_reg_address(w, GUM_X86_RAX, (GumAddress)(uintptr_t)agy_gomod_ensure);
        gum_x86_writer_put_call_reg(w, GUM_X86_RAX);

        if (asmcgo_on) {
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
            gum_x86_writer_put_mov_reg_address(w, GUM_X86_R12, (GumAddress)asmcgocall_abs);
            gum_x86_writer_put_call_reg(w, GUM_X86_R12);
            gum_x86_writer_put_add_reg_imm(w, GUM_X86_RSP, 32);
        } else {
            /* cgocall(fn=RAX, arg=RBX): arg=&block, fn=&agy_cgo_hook; X15 must be 0.
             * The 16-byte GH_SPILL scratch below the block absorbs cgocall's two
             * caller-arg spill slots ([S],[S+8]), so block.kind/regs.rax stay intact. */
            gum_x86_writer_put_lea_reg_reg_offset(w, GUM_X86_RBX, GUM_X86_RSP, OFF_KIND);
            gum_x86_writer_put_mov_reg_address(w, GUM_X86_RAX, (GumAddress)(uintptr_t)agy_cgo_hook);
            gum_x86_writer_put_bytes(w, XORPS_XMM15, sizeof XORPS_XMM15);
            gum_x86_writer_put_mov_reg_address(w, GUM_X86_R12, (GumAddress)cgocall_abs);
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
        frame_lo = lo; frame_hi = hi;   /* identical for every slot (fixed pro/epilogue) */

        /* patch target+skip: jmp rel32 to the slot, nop-pad to the whole prologue */
        struct patch_ctx pc = { .n = ov };
        memset(pc.bytes, 0x90, ov);
        int32_t rel = (int32_t)((int64_t)(uintptr_t)slot - (int64_t)(hook_addr + 5));
        pc.bytes[0] = 0xe9;
        memcpy(pc.bytes + 1, &rel, 4);
        if (!gum_memory_patch_code((gpointer)(uintptr_t)hook_addr, ov, patch_apply, &pc)) {
            /* target prologue wasn't overwritten → the trampoline is unreachable. Don't
             * count it (the moduledata below covers only `made` slots) and reuse this
             * slot offset on the next target. */
            GHLOG("patch failed for kind=%s @ +%u — trampoline NOT installed, skipping",
                  targets[i].kind, targets[i].skip);
            continue;
        }

        GHLOG("cgo-trampoline kind=%s @ +%u -> %p (overwrite %u bytes)",
              targets[i].kind, targets[i].skip, (void *)slot, ov);
        made++;
    }
    gum_x86_writer_unref(w);
    if (!made) { GHLOG("no trampolines installed"); return 0; }

    /* GC-unwind safety: BUILD the covering synthetic moduledata now (constructor
     * time; no firstmoduledata read). The trampolines call agy_gomod_ensure() to
     * SPLICE it lazily on first hit, when Go is up. Required — without it a GC that
     * unwinds a trampoline frame throws("unknown pc"). */
    uintptr_t rbase = (uintptr_t)region;
    int rc = agy_gomod_prepare(base + md_vaddr, cgocall_abs,
                               rbase, rbase + (uintptr_t)made * GH_SLOT,
                               GH_SLOT, made, GH_FRAME, frame_lo, frame_hi);
    if (rc != 0) GHLOG("moduledata prepare failed (rc=%d); trampolines are live "
                       "but NOT GC-safe — expect throw(\"unknown pc\") under GC", rc);
    return made;
}
