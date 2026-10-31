/* The cgocall trampoline stubs: the Go→C bridge, written once as AGY_STUB.
 *
 * make_libagy.py patches each hooked function's `entry + skip` with `jmp *agy_tramp_slots[slot]`,
 * and ld.so points the slot at agy_stub__<id>, which runs on the goroutine's stack with the Go
 * register-ABI arguments live. While its row is on (g_agy_on), a stub saves them into the
 * goframe.h frame, runs agy_dispatch through runtime.cgocall or runtime.asmcgocall and restores
 * them. It then runs the displaced prologue and resumes the function, or returns if the handler
 * set block.action.
 * X15 is re-zeroed after C code, as Go's ABI requires.
 *
 * Naked, because the body has no C equivalent; that allows only basic asm, so the C symbols it
 * names carry AGY_ASM_REFERENCED (gocall.h). __builtin_unreachable() stops GCC appending a ud2
 * past the slot, and -fno-lto (CMakeLists.txt) keeps LTO from merging or moving stubs.
 */
#include "goframe.h"
#include "procdef.h"
#include "symbols_gen.h"

#define TO_STRING_IMPL(x) #x
#define TO_STRING(x) TO_STRING_IMPL(x)

/* id: the row id; book: its BOOKS index, which gates the stub and goes in block.book;
 * gateway: agy_go__CGOCALL or agy_go__ASMCGOCALL; resume_off: skip + ow; ...: the displaced bytes. */
#define AGY_STUB(id, book, gateway, resume_off, ...)                                          \
    static_assert(bk(#id) == (book), "symbols_gen.h is out of step with BOOKS");             \
    extern "C" __attribute__((naked, used, visibility("default"),                            \
                              aligned(AGY_SLOT_SIZE), section(AGY_TRAMP_SECTION)))           \
    void agy_stub__##id() {                                                                  \
        __asm__(                                                                             \
            "cmpb   $0, g_agy_on+" #book "(%rip)\n\t"                                        \
            "je     1f\n\t"                                                                  \
            "subq   $" TO_STRING(AGY_FRAME) ", %rsp\n\t"                                     \
            "movq   %rax, " TO_STRING(AGY_OFF_RAX) "(%rsp)\n\t"                              \
            "movq   %rbx, " TO_STRING(AGY_OFF_RBX) "(%rsp)\n\t"                              \
            "movq   %rcx, " TO_STRING(AGY_OFF_RCX) "(%rsp)\n\t"                              \
            "movq   %rdi, " TO_STRING(AGY_OFF_RDI) "(%rsp)\n\t"                              \
            "movq   %rsi, " TO_STRING(AGY_OFF_RSI) "(%rsp)\n\t"                              \
            "movq   %r8,  " TO_STRING(AGY_OFF_R8)  "(%rsp)\n\t"                              \
            "movq   %r9,  " TO_STRING(AGY_OFF_R9)  "(%rsp)\n\t"                              \
            "movq   %r10, " TO_STRING(AGY_OFF_R10) "(%rsp)\n\t"                              \
            "movq   %r11, " TO_STRING(AGY_OFF_R11) "(%rsp)\n\t"                              \
            "movq   %rdx, " TO_STRING(AGY_OFF_RDX) "(%rsp)\n\t"                              \
            "movq   %rbp, " TO_STRING(AGY_OFF_RBP) "(%rsp)\n\t"                              \
            "movq   $" #book ", " TO_STRING(AGY_OFF_BOOK) "(%rsp)\n\t"                       \
            "xorl   %eax, %eax\n\t"                                                          \
            "movq   %rax, " TO_STRING(AGY_OFF_ACTION) "(%rsp)\n\t"                           \
            "leaq   " TO_STRING(AGY_OFF_BLOCK) "(%rsp), %rbx\n\t"                            \
            "leaq   agy_dispatch(%rip), %rax\n\t"                                            \
            "movq   %rax, " TO_STRING(AGY_OFF_FN) "(%rsp)\n\t"                               \
            "movq   %rbx, " TO_STRING(AGY_OFF_ARG) "(%rsp)\n\t"                              \
            "xorps  %xmm15, %xmm15\n\t"                                                      \
            "call   *" #gateway "@GOTPCREL(%rip)\n\t"                                        \
            "movq   " TO_STRING(AGY_OFF_RAX) "(%rsp), %rax\n\t"                              \
            "movq   " TO_STRING(AGY_OFF_RBX) "(%rsp), %rbx\n\t"                              \
            "movq   " TO_STRING(AGY_OFF_RCX) "(%rsp), %rcx\n\t"                              \
            "movq   " TO_STRING(AGY_OFF_RDI) "(%rsp), %rdi\n\t"                              \
            "movq   " TO_STRING(AGY_OFF_RSI) "(%rsp), %rsi\n\t"                              \
            "movq   " TO_STRING(AGY_OFF_R8)  "(%rsp), %r8\n\t"                               \
            "movq   " TO_STRING(AGY_OFF_R9)  "(%rsp), %r9\n\t"                               \
            "movq   " TO_STRING(AGY_OFF_R10) "(%rsp), %r10\n\t"                              \
            "movq   " TO_STRING(AGY_OFF_R11) "(%rsp), %r11\n\t"                              \
            "movq   " TO_STRING(AGY_OFF_RDX) "(%rsp), %rdx\n\t"                              \
            "movq   " TO_STRING(AGY_OFF_RBP) "(%rsp), %rbp\n\t"                              \
            "movq   " TO_STRING(AGY_OFF_ACTION) "(%rsp), %r12\n\t"                           \
            "addq   $" TO_STRING(AGY_FRAME) ", %rsp\n\t"                                     \
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
            ".balign " TO_STRING(AGY_SLOT_SIZE) ", 0x90");                                   \
        __builtin_unreachable();                                                             \
    }

/* One stub per installed row. Each is slot-aligned and pads itself, so they tile the section;
 * one outgrowing its slot is caught by agy_init(), as branch sizes aren't known here. */
AGY_STUBS(AGY_STUB)
