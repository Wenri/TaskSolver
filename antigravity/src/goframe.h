/* The stub frame layout, shared by goframe.cpp's asm and the C++ that reads it as agy_block
 * (src/gomod.h). Basic asm can't use offsetof, so the offsets are literals, and gomod.cpp
 * static_asserts each one against agy_block.
 *
 *   rsp+0    handler fn      \ the two words runtime.cgocall / runtime.asmcgocall consume
 *   rsp+8    &block          /
 *   rsp+16   block.book      <- agy_block starts here
 *   rsp+24   block.regs.rax .. rsp+104 block.regs.rbp   (Go's register ABI, 11 words)
 *   rsp+112  block.action    <- 0 = PASS (run the body), nonzero = RETURN
 *   rsp+120  (caller)
 */
#ifndef AGY_GOFRAME_H
#define AGY_GOFRAME_H

#define AGY_FRAME        120   /* bytes the stub subtracts from rsp */
#define AGY_SLOT_SIZE    256   /* stride between stubs */
#define AGY_TRAMP_SECTION "agy_trampolines"   /* a C identifier, so ld defines __start_/__stop_ */

/* Where in a slot the frame is live (after the subq, up to the addq), for the synthetic pcsp. */
#define AGY_FRAME_LO      17
#define AGY_FRAME_HI     179

#define AGY_OFF_FN         0   /* cgocall arg 0: the C handler */
#define AGY_OFF_ARG        8   /* cgocall arg 1: &block */
#define AGY_OFF_BLOCK     16

#define AGY_OFF_BOOK      16   /* block.book: the row's BOOKS index */
#define AGY_OFF_RAX       24   /* block.regs.*, in Go register ABI order */
#define AGY_OFF_RBX       32
#define AGY_OFF_RCX       40
#define AGY_OFF_RDI       48
#define AGY_OFF_RSI       56
#define AGY_OFF_R8        64
#define AGY_OFF_R9        72
#define AGY_OFF_R10       80
#define AGY_OFF_R11       88
#define AGY_OFF_RDX       96
#define AGY_OFF_RBP      104
#define AGY_OFF_ACTION   112   /* block.action */

/* The trampoline region, bounded by the linker. */
#ifdef __cplusplus
extern "C" {
#endif
extern const unsigned char __start_agy_trampolines[];
extern const unsigned char __stop_agy_trampolines[];
#ifdef __cplusplus
}
#endif

#define agy_tramp_region_start __start_agy_trampolines
#define agy_tramp_region_end   __stop_agy_trampolines

#endif /* AGY_GOFRAME_H */
