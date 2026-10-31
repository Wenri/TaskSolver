/* The static cgocall trampoline stubs — the Go→C bridge, written once as a macro.
 *
 * libagy.so's hooked Go functions are statically patched (by symbols/make_libagy.py) at
 * `entry + skip` with `jmp *agy_tramp_slots[slot](%rip)`, and ld.so fills each slot with the
 * address of the matching `agy_stub__<ID>` below. A stub therefore runs ON the hooked goroutine's
 * stack, in Go's register ABI, with the original arguments still live in registers.
 *
 * Each stub:
 *   1. bails straight to the displaced prologue when its enable flag is clear (zero added cost
 *      once hooks are off),
 *   2. otherwise saves the Go register ABI's argument/scratch registers into a 120-byte frame
 *      laid out as `agy_block` (see src/gocall.h), hands `&block` to the dedicated C handler
 *      through runtime.cgocall / runtime.asmcgocall, and restores them,
 *   3. runs the frame-setup instructions it displaced, then jumps back into the hooked function
 *      just past them — unless the handler asked to return early (%r12 non-zero), in which case
 *      it returns with the handler's values in place.
 *
 * `xorps %xmm15, %xmm15` before the gateway call and before `ret`: Go's ABI requires X15 to be
 * zero, and the C handler may have clobbered it.
 *
 * The per-hook parameters (which handler, which gateway, which enable flag, how far to resume,
 * and which prologue bytes were displaced) come from symbols/gen_sources.py as one AGY_STUB line
 * per hook in trampolines_stubs.inc. Nothing here is specific to an agy release; the numbers all
 * derive from src/procdef.h plus symbols/symbols.json.
 *
 * Slots are a fixed stride apart so src/gomod.cpp can describe the whole region to Go's runtime
 * as one synthetic function; unused tail bytes are NOP-padded.
 */

/* ---- one stub ---------------------------------------------------------------------------------
 * id          hook id; defines agy_stub__<id> and resumes into agy_go__<id>
 * slot        index in agy_tramp_slots[]; also fixes this stub's offset in the region
 * enable      byte flag gating the hook (g_agy_enabled, or g_agy_conv_id for FILE_OPEN)
 * handler     dedicated C handler (agy_on_*), called via the gateway
 * gateway     agy_go__CGOCALL (parking, GC-safe) or agy_go__ASMCGOCALL (lighter g0 switch)
 * resume_off  skip + ow: where to re-enter the hooked function, past the displaced prologue
 * prologue    the displaced frame-setup bytes themselves (8 or 11 of them)
 */
#include "agy_frame.h"

.macro AGY_STUB id, slot, enable, handler, gateway, resume_off, prologue:vararg
    .globl  agy_stub__\id
    .type   agy_stub__\id, @function
agy_stub__\id:
    cmpb    $0, \enable(%rip)
    je      .Lpass__\slot
    subq    $AGY_FRAME, %rsp
.Lframe_lo__\slot:
    movq    %rax, AGY_OFF_RAX(%rsp)
    movq    %rbx, AGY_OFF_RBX(%rsp)
    movq    %rcx, AGY_OFF_RCX(%rsp)
    movq    %rdi, AGY_OFF_RDI(%rsp)
    movq    %rsi, AGY_OFF_RSI(%rsp)
    movq    %r8,  AGY_OFF_R8(%rsp)
    movq    %r9,  AGY_OFF_R9(%rsp)
    movq    %r10, AGY_OFF_R10(%rsp)
    movq    %r11, AGY_OFF_R11(%rsp)
    movq    %rdx, AGY_OFF_RDX(%rsp)
    movq    %rbp, AGY_OFF_RBP(%rsp)
    leaq    .Lkind__\slot(%rip), %rax
    movq    %rax, AGY_OFF_KIND(%rsp)
    xorl    %eax, %eax
    movq    %rax, AGY_OFF_ACTION(%rsp)
    leaq    AGY_OFF_BLOCK(%rsp), %rbx
    leaq    \handler(%rip), %rax
    movq    %rax, AGY_OFF_FN(%rsp)
    movq    %rbx, AGY_OFF_ARG(%rsp)
    xorps   %xmm15, %xmm15
    call    *\gateway@GOTPCREL(%rip)
    movq    AGY_OFF_RAX(%rsp), %rax
    movq    AGY_OFF_RBX(%rsp), %rbx
    movq    AGY_OFF_RCX(%rsp), %rcx
    movq    AGY_OFF_RDI(%rsp), %rdi
    movq    AGY_OFF_RSI(%rsp), %rsi
    movq    AGY_OFF_R8(%rsp), %r8
    movq    AGY_OFF_R9(%rsp), %r9
    movq    AGY_OFF_R10(%rsp), %r10
    movq    AGY_OFF_R11(%rsp), %r11
    movq    AGY_OFF_RDX(%rsp), %rdx
    movq    AGY_OFF_RBP(%rsp), %rbp
    movq    AGY_OFF_ACTION(%rsp), %r12
.Lframe_hi__\slot:
    addq    $AGY_FRAME, %rsp
    testq   %r12, %r12
    jne     .Lret__\slot
.Lpass__\slot:
    .byte   \prologue
    movq    agy_go__\id@GOTPCREL(%rip), %r12
    leaq    \resume_off(%r12), %r12
    jmp     *%r12
.Lret__\slot:
    xorps   %xmm15, %xmm15
    ret
    .org    agy_tramp_region_start + (\slot + 1) * AGY_SLOT_SIZE, 0x90
.endm

/* ---- the region ------------------------------------------------------------------------------
 * trampolines_gen.inc is generated beside symbols_gen.h: it sets AGY_FRAME / AGY_SLOT_SIZE, emits
 * the per-hook kind strings into .rodata.agy_trampolines, and expands AGY_STUB once per installed
 * hook, in slot order, into the section opened here.
 */
.pushsection .agy_trampolines,"ax",@progbits
    .p2align 8
    .globl  agy_tramp_region_start
    .hidden agy_tramp_region_start
agy_tramp_region_start:

#include "trampolines_gen.inc"

    .globl  agy_tramp_region_end
    .hidden agy_tramp_region_end
agy_tramp_region_end:
.popsection

.section .note.GNU-stack,"",@progbits
