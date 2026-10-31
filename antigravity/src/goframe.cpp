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
 *   2. otherwise saves the Go register ABI's argument/scratch registers into the frame described
 *      by goframe.h, hands `&block` to the dedicated C handler through runtime.cgocall /
 *      runtime.asmcgocall, and restores them,
 *   3. runs the frame-setup instructions it displaced, then jumps back into the hooked function
 *      just past them — unless the handler asked to return early (`action` non-zero), in which
 *      case it returns with the handler's values in place.
 *
 * `xorps %xmm15, %xmm15` before the gateway call and before `ret`: Go's ABI requires X15 to be
 * zero, and the C handler may have clobbered it.
 *
 * WHY NAKED FUNCTIONS, AND WHAT THAT COSTS
 * A stub has no C-expressible body: it runs in Go's register ABI, must not touch the stack before
 * it has saved those registers, splices instruction bytes displaced out of another function, and
 * leaves by jumping into the middle of one. `naked` is what buys that — the compiler emits no
 * prologue, no epilogue and no frame — and a naked body may contain only *basic* asm, so there are
 * no operand constraints and every symbol is named directly. Three consequences worth knowing:
 *
 *   - `__builtin_unreachable()` after each body is load-bearing, not decoration. Without it GCC
 *     appends a `ud2` after the asm, which lands past this stub's `.org` padding and pushes the
 *     next stub off its slot boundary.
 *   - this file is compiled `-fno-lto` (see CMakeLists.txt). There is nothing here to optimise,
 *     and it keeps LTO from folding two stubs it considers identical or moving them out of the
 *     section, either of which would break the one-stub-per-slot layout.
 *   - the C handlers and flags named in the asm are invisible to the optimiser, so they carry
 *     AGY_ASM_REFERENCED (gocall.h).
 *
 * The per-hook parameters (which handler, which gateway, which enable flag, how far to resume,
 * and which prologue bytes were displaced) come from symbols/gen_sources.py as one AGY_STUB line
 * per hook in goframe_gen.h. Nothing here is specific to an agy release; the numbers all derive
 * from src/procdef.h plus symbols/symbols.json.
 *
 * Slots are a fixed stride apart so src/gomod.cpp can describe the whole region to Go's runtime
 * as one synthetic function; unused tail bytes are NOP-padded.
 */
#include "goframe.h"
#include "gocall.h"
#include "symbols_gen.h"

#define AGY_S_(x)   #x
#define AGY_S(x)    AGY_S_(x)

/* ---- one stub ---------------------------------------------------------------------------------
 * id          hook id; defines agy_stub__<id> and resumes into agy_go__<id>
 * slot        index in agy_tramp_slots[]; also fixes this stub's offset in the region
 * enable      byte flag gating the hook (g_agy_enabled, or g_agy_conv_id for FILE_OPEN)
 * handler     dedicated C handler (agy_on_*), called via the gateway
 * gateway     agy_go__CGOCALL (parking, GC-safe) or agy_go__ASMCGOCALL (lighter g0 switch)
 * resume_off  skip + ow: where to re-enter the hooked function, past the displaced prologue
 * ...         the displaced frame-setup bytes themselves (8 or 11 of them)
 */
#define AGY_STUB(id, slot, enable, handler, gateway, resume_off, ...)                        \
    extern "C" __attribute__((naked, used, visibility("default"),                            \
                              aligned(AGY_SLOT_SIZE), section(AGY_TRAMP_SECTION)))           \
    void agy_stub__##id() {                                                                  \
        __asm__(                                                                             \
            "cmpb   $0, " #enable "(%rip)\n\t"                                               \
            "je     1f\n\t"                                                                  \
            "subq   $" AGY_S(AGY_FRAME) ", %rsp\n\t"                                         \
            "movq   %rax, " AGY_S(AGY_OFF_RAX) "(%rsp)\n\t"                                  \
            "movq   %rbx, " AGY_S(AGY_OFF_RBX) "(%rsp)\n\t"                                  \
            "movq   %rcx, " AGY_S(AGY_OFF_RCX) "(%rsp)\n\t"                                  \
            "movq   %rdi, " AGY_S(AGY_OFF_RDI) "(%rsp)\n\t"                                  \
            "movq   %rsi, " AGY_S(AGY_OFF_RSI) "(%rsp)\n\t"                                  \
            "movq   %r8,  " AGY_S(AGY_OFF_R8)  "(%rsp)\n\t"                                  \
            "movq   %r9,  " AGY_S(AGY_OFF_R9)  "(%rsp)\n\t"                                  \
            "movq   %r10, " AGY_S(AGY_OFF_R10) "(%rsp)\n\t"                                  \
            "movq   %r11, " AGY_S(AGY_OFF_R11) "(%rsp)\n\t"                                  \
            "movq   %rdx, " AGY_S(AGY_OFF_RDX) "(%rsp)\n\t"                                  \
            "movq   %rbp, " AGY_S(AGY_OFF_RBP) "(%rsp)\n\t"                                  \
            "leaq   .Lkind__" #slot "(%rip), %rax\n\t"                                       \
            "movq   %rax, " AGY_S(AGY_OFF_KIND) "(%rsp)\n\t"                                 \
            "xorl   %eax, %eax\n\t"                                                          \
            "movq   %rax, " AGY_S(AGY_OFF_ACTION) "(%rsp)\n\t"                               \
            "leaq   " AGY_S(AGY_OFF_BLOCK) "(%rsp), %rbx\n\t"                                \
            "leaq   " #handler "(%rip), %rax\n\t"                                            \
            "movq   %rax, " AGY_S(AGY_OFF_FN) "(%rsp)\n\t"                                   \
            "movq   %rbx, " AGY_S(AGY_OFF_ARG) "(%rsp)\n\t"                                  \
            "xorps  %xmm15, %xmm15\n\t"                                                      \
            "call   *" #gateway "@GOTPCREL(%rip)\n\t"                                        \
            "movq   " AGY_S(AGY_OFF_RAX) "(%rsp), %rax\n\t"                                  \
            "movq   " AGY_S(AGY_OFF_RBX) "(%rsp), %rbx\n\t"                                  \
            "movq   " AGY_S(AGY_OFF_RCX) "(%rsp), %rcx\n\t"                                  \
            "movq   " AGY_S(AGY_OFF_RDI) "(%rsp), %rdi\n\t"                                  \
            "movq   " AGY_S(AGY_OFF_RSI) "(%rsp), %rsi\n\t"                                  \
            "movq   " AGY_S(AGY_OFF_R8)  "(%rsp), %r8\n\t"                                   \
            "movq   " AGY_S(AGY_OFF_R9)  "(%rsp), %r9\n\t"                                   \
            "movq   " AGY_S(AGY_OFF_R10) "(%rsp), %r10\n\t"                                  \
            "movq   " AGY_S(AGY_OFF_R11) "(%rsp), %r11\n\t"                                  \
            "movq   " AGY_S(AGY_OFF_RDX) "(%rsp), %rdx\n\t"                                  \
            "movq   " AGY_S(AGY_OFF_RBP) "(%rsp), %rbp\n\t"                                  \
            "movq   " AGY_S(AGY_OFF_ACTION) "(%rsp), %r12\n\t"                               \
            "addq   $" AGY_S(AGY_FRAME) ", %rsp\n\t"                                         \
            "testq  %r12, %r12\n\t"                                                          \
            "jne    2f\n"                                                                    \
            "1:\n\t"                                                                         \
            ".byte  " #__VA_ARGS__ "\n\t"                                                    \
            "movq   agy_go__" #id "@GOTPCREL(%rip), %r12\n\t"                                \
            "leaq   " #resume_off "(%r12), %r12\n\t"                                          \
            "jmp    *%r12\n"                                                                 \
            "2:\n\t"                                                                         \
            "xorps  %xmm15, %xmm15\n\t"                                                      \
            "ret\n\t"                                                                        \
            ".balign " AGY_S(AGY_SLOT_SIZE) ", 0x90");                                       \
        __builtin_unreachable();   /* suppress GCC's trailing ud2 — see the note above */     \
    }

/* goframe_gen.h is generated beside symbols_gen.h: the per-hook kind strings, then one AGY_STUB
 * per installed hook.
 *
 * Placement needs no anchor and no region label. Each stub is `aligned` to the slot stride and
 * pads its own tail with `.balign`, so the stubs tile the section exactly and the linker's
 * __start_/__stop_ symbols (goframe.h) are the true bounds.
 *
 * A stub outgrowing its slot would silently widen the stride, and it cannot be caught here: the
 * body's forward branches are not sized until relaxation, so `.` is not a constant the assembler
 * can test. agy_init() checks the aggregate instead — the section must measure exactly
 * nslots * stride — which fails for exactly the same reason, at startup, with a clear message. */
#include "goframe_gen.h"

