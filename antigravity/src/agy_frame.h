/* The stub frame layout — the one place the assembly and the C++ agree on it.
 *
 * src/trampolines.S builds this frame on the hooked goroutine's stack and hands `&block` to a C
 * handler; gocall.cpp reads it back as `agy_block` (src/gomod.h). The assembly cannot use
 * `offsetof`, so the offsets below are literals — but because a `.S` runs through the C
 * preprocessor, both sides include this header, and gomod.cpp static_asserts every offset against
 * the real struct. Add or reorder a field in `agy_go_regs` and the build stops here rather than
 * silently writing registers to the wrong slots.
 *
 *   rsp+0    handler fn      \ the two words runtime.cgocall / runtime.asmcgocall consume
 *   rsp+8    &block          /
 *   rsp+16   block.kind      <- agy_block starts here
 *   rsp+24   block.regs.rax .. rsp+104 block.regs.rbp   (Go's register ABI, 11 words)
 *   rsp+112  block.action    <- 0 = PASS (run the body), nonzero = RETURN
 *   rsp+120  (caller)
 */
#ifndef AGY_FRAME_H
#define AGY_FRAME_H

#define AGY_FRAME        120   /* total bytes the stub subtracts from rsp */
#define AGY_SLOT_SIZE    256   /* stride between stubs in .agy_trampolines */

/* Offsets of the live frame within a slot, for the synthetic moduledata's locals bitmap:
 * the region where the frame is established, so Go's GC scans it conservatively. */
#define AGY_FRAME_LO      17
#define AGY_FRAME_HI     182

#define AGY_OFF_FN         0   /* cgocall arg 0: the C handler to run on g0 */
#define AGY_OFF_ARG        8   /* cgocall arg 1: &block */
#define AGY_OFF_BLOCK     16   /* agy_block itself */

#define AGY_OFF_KIND      16   /* block.kind   — borrowed const char* (procdef kind tag) */
#define AGY_OFF_RAX       24   /* block.regs.* — Go register ABI argument order */
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
#define AGY_OFF_ACTION   112   /* block.action — the hook's PASS/RETURN verdict */

#endif /* AGY_FRAME_H */
