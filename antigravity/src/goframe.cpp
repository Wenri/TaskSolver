/* The Go→C trampoline stubs: FULLCGO uses register arguments; ASMCGO uses stack arguments.
 *
 * make_libagy.py patches each hooked function's `entry + skip` with E9 rel32 to .plt.agy.
 * Its PLT entry jumps through a .got.plt.agy slot eagerly resolved by ld.so via JUMP_SLOT
 * to agy_stub__<id>, which runs on the goroutine's stack with the Go
 * register-ABI arguments live. A stub loads g_agy_on[book] once into the Go scratch register
 * R12; null skips the handler. Otherwise it saves the arguments into the goframe.h frame,
 * calls that handler through runtime.cgocall or runtime.asmcgocall, and restores the registers.
 * It runs the displaced prologue and resumes the function, or returns if the handler set block.action.
 * Go supplies zero in X15 at entry; re-zero it after C code on both PASS and RETURN paths.
 *
 * Naked functions use basic asm. The table it names carries used and externally_visible;
 * __builtin_unreachable() stops GCC appending a ud2 past the end label; -fno-lto preserves
 * the asm-defined labels referenced by the metadata descriptors.
 * test_agy_stubs.py checks the generated instructions, frame PCs and native startup.
 */
#include "goframe.h"
#include "gocall.h"
#include "procdef.h"
#include "symbols_gen.h"

static_assert(sizeof(agy_book_fn) == 8, "stub table indexing requires 8-byte handler pointers");
static_assert(std::size(BOOKS) <= INT32_MAX, "movq's signed immediate must zero the adjacent action");

#define TO_STRING_IMPL(x) #x
#define TO_STRING(x) TO_STRING_IMPL(x)

#define AGY_PC(id, part) \
    ".globl agy_pc__" #id "_" #part "\n" \
    ".hidden agy_pc__" #id "_" #part "\n" \
    "agy_pc__" #id "_" #part ":\n\t"

/* Shared register snapshot and continuation. One qword initializes book and action=0.
 * CMP reads action while the frame is live; LEA releases the frame without changing flags. */
#define AGY_STUB_ENTER(id, book) \
    "movq   g_agy_on+8*" #book "(%rip), %r12\n\t" \
    "testq  %r12, %r12\n\t" \
    "je     1f\n\t" \
    "subq   $" TO_STRING(AGY_FRAME) ", %rsp\n\t" \
    AGY_PC(id, frame_start) \
    "movq   %rax, " TO_STRING(AGY_OFF_RAX) "(%rsp)\n\t" \
    "movq   %rbx, " TO_STRING(AGY_OFF_RBX) "(%rsp)\n\t" \
    "movq   %rcx, " TO_STRING(AGY_OFF_RCX) "(%rsp)\n\t" \
    "movq   %rdi, " TO_STRING(AGY_OFF_RDI) "(%rsp)\n\t" \
    "movq   %rsi, " TO_STRING(AGY_OFF_RSI) "(%rsp)\n\t" \
    "movq   %r8,  " TO_STRING(AGY_OFF_R8)  "(%rsp)\n\t" \
    "movq   %r9,  " TO_STRING(AGY_OFF_R9)  "(%rsp)\n\t" \
    "movq   %r10, " TO_STRING(AGY_OFF_R10) "(%rsp)\n\t" \
    "movq   %r11, " TO_STRING(AGY_OFF_R11) "(%rsp)\n\t" \
    "movq   %rdx, " TO_STRING(AGY_OFF_RDX) "(%rsp)\n\t" \
    "movq   %rbp, " TO_STRING(AGY_OFF_RBP) "(%rsp)\n\t" \
    "movq   $" #book ", " TO_STRING(AGY_OFF_BOOK) "(%rsp)\n\t"

#define AGY_STUB_LEAVE(id, go_frame) \
    "xorps  %xmm15, %xmm15\n\t" \
    "movq   " TO_STRING(AGY_OFF_RAX) "(%rsp), %rax\n\t" \
    "movq   " TO_STRING(AGY_OFF_RBX) "(%rsp), %rbx\n\t" \
    "movq   " TO_STRING(AGY_OFF_RCX) "(%rsp), %rcx\n\t" \
    "movq   " TO_STRING(AGY_OFF_RDI) "(%rsp), %rdi\n\t" \
    "movq   " TO_STRING(AGY_OFF_RSI) "(%rsp), %rsi\n\t" \
    "movq   " TO_STRING(AGY_OFF_R8)  "(%rsp), %r8\n\t" \
    "movq   " TO_STRING(AGY_OFF_R9)  "(%rsp), %r9\n\t" \
    "movq   " TO_STRING(AGY_OFF_R10) "(%rsp), %r10\n\t" \
    "movq   " TO_STRING(AGY_OFF_R11) "(%rsp), %r11\n\t" \
    "movq   " TO_STRING(AGY_OFF_RDX) "(%rsp), %rdx\n\t" \
    "movq   " TO_STRING(AGY_OFF_RBP) "(%rsp), %rbp\n\t" \
    "cmpl   $0, " TO_STRING(AGY_OFF_ACTION) "(%rsp)\n\t" \
    "leaq   " TO_STRING(AGY_FRAME) "(%rsp), %rsp\n\t" \
    AGY_PC(id, frame_end) \
    "jne    2f\n" \
    "1:\n\t" \
    "pushq  %rbp\n\t" \
    "movq   %rsp, %rbp\n\t" \
    "subq   $" #go_frame ", %rsp\n\t" \
    "jmp    *agy_resume__" #id "@GOTPCREL(%rip)\n" \
    "2:\n\t" \
    "ret\n\t" \
    AGY_PC(id, end)

#define AGY_STUB_DECLARE(id, book) \
    static_assert(bk(#id) == (book), "symbols_gen.h is out of step with BOOKS"); \
    extern "C" { \
        extern const unsigned char agy_pc__##id##_frame_start[]; \
        extern const unsigned char agy_pc__##id##_frame_end[]; \
        extern const unsigned char agy_pc__##id##_end[]; \
    } \
    extern "C" __attribute__((naked, used, visibility("default"))) \
    void agy_stub__##id()

/* ABIInternal: cgocall spills its own RAX/RBX arguments into our reserved two words. */
#define AGY_STUB_FULLCGO(id, book, go_frame) \
    AGY_STUB_DECLARE(id, book) { \
        __asm__(AGY_STUB_ENTER(id, book) \
            "leaq   " TO_STRING(AGY_OFF_BLOCK) "(%rsp), %rbx\n\t" \
            "movq   %r12, %rax\n\t" \
            "call   *agy_go__CGOCALL@GOTPCREL(%rip)\n\t" \
            AGY_STUB_LEAVE(id, go_frame)); \
        __builtin_unreachable(); \
    }

/* ABI0: asmcgocall reads fn/arg from the stack; it does not need register copies. */
#define AGY_STUB_ASMCGO(id, book, go_frame) \
    AGY_STUB_DECLARE(id, book) { \
        __asm__(AGY_STUB_ENTER(id, book) \
            "movq   %r12, " TO_STRING(AGY_OFF_FN) "(%rsp)\n\t" \
            "leaq   " TO_STRING(AGY_OFF_BLOCK) "(%rsp), %rax\n\t" \
            "movq   %rax, " TO_STRING(AGY_OFF_ARG) "(%rsp)\n\t" \
            "call   *agy_go__ASMCGOCALL@GOTPCREL(%rip)\n\t" \
            AGY_STUB_LEAVE(id, go_frame)); \
        __builtin_unreachable(); \
    }

/* The linker may place these functions in any order, with gaps between them. */
AGY_STUBS(AGY_STUB_FULLCGO, AGY_STUB_ASMCGO)

#define AGY_STUB_INFO(id, book, go_frame) \
    {agy_stub__##id, agy_pc__##id##_frame_start, agy_pc__##id##_frame_end, agy_pc__##id##_end},
const agy_stub_info agy_stubs[] = { AGY_STUBS(AGY_STUB_INFO, AGY_STUB_INFO) };
static_assert(std::size(agy_stubs) == BK_STUB_COUNT, "one descriptor per installed hook");
