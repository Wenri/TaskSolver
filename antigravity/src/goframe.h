/* The stub frame layout, shared by goframe.cpp's constexpr asm builder and the C++
 * that reads it as agy_block (src/gomod.h). gomod.cpp checks these offsets against
 * agy_block with static_assert.
 *
 *   rsp+0    handler fn      \ runtime.cgocall spill space / runtime.asmcgocall arguments
 *   rsp+8    &block          /
 *   rsp+16   block.book      <- uint32; also asmcgocall's int32 return slot, dead after the call
 *   rsp+20   block.action    <- uint32; 0 = PASS (run the body), nonzero = RETURN
 *   rsp+24   block.regs.rax .. rsp+104 block.regs.rbp   (Go's register ABI, 11 words)
 *   rsp+112  (caller)
 */
#ifndef AGY_GOFRAME_H
#define AGY_GOFRAME_H

#include "procdef.h"

#define AGY_FRAME        112   /* bytes the stub subtracts from rsp */

#define AGY_OFF_FN         0   /* cgocall arg 0: the C handler */
#define AGY_OFF_ARG        8   /* cgocall arg 1: &block */
#define AGY_OFF_BLOCK     16

#define AGY_OFF_BOOK      16   /* block.book: the row's BOOKS index */
#define AGY_OFF_ACTION    20   /* block.action */
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

/* Linked function boundaries, independent of section placement or function order.
 * The frame is live after SUB through the end of LEA; the end label follows RET. */
struct agy_stub_info {
    void (*entry)(void);
    const unsigned char *frame_start, *frame_end, *end;
};

/* Defined in goframe.cpp, one entry per stub, in BOOKS order. */
extern const agy_stub_info agy_stubs[BK_STUB_COUNT];

#endif /* AGY_GOFRAME_H */
