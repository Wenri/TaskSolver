/* The Go→C trampoline stubs: FULLCGO uses register arguments; ASMCGO uses stack arguments.
 *
 * make_libagy.py patches each hooked function's `entry + skip` with `jmp *agy_tramp_slots[slot]`,
 * and ld.so points the slot at agy_stub__<id>, which runs on the goroutine's stack with the Go
 * register-ABI arguments live. A stub loads g_agy_on[book] once into the Go scratch register
 * R12; null skips the handler. Otherwise it saves the arguments into the goframe.h frame,
 * calls that handler through runtime.cgocall or runtime.asmcgocall, and restores the registers.
 * It runs the displaced prologue and resumes the function, or returns if the handler set block.action.
 * Go supplies zero in X15 at entry; re-zero it after C code on both PASS and RETURN paths.
 *
 * Naked functions use basic asm. The table it names carries used and externally_visible;
 * __builtin_unreachable() stops GCC appending a ud2 past the slot, and -fno-lto keeps stubs ordered.
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

/* Shared register snapshot and continuation. One qword initializes book and action=0.
 * CMP reads action while the frame is live; LEA releases the frame without changing flags. */
#define AGY_STUB_ENTER(book) \
    "movq   g_agy_on+8*" #book "(%rip), %r12\n\t" \
    "testq  %r12, %r12\n\t" \
    "je     1f\n\t" \
    "subq   $" TO_STRING(AGY_FRAME) ", %rsp\n\t" \
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

#define AGY_STUB_LEAVE(id, resume_off, ...) \
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
    "jne    2f\n" \
    "1:\n\t" \
    ".byte  " #__VA_ARGS__ "\n\t" \
    "movq   agy_go__" #id "@GOTPCREL(%rip), %r12\n\t" \
    "leaq   " #resume_off "(%r12), %r12\n\t" \
    "jmp    *%r12\n" \
    "2:\n\t" \
    "ret\n\t" \
    ".balign " TO_STRING(AGY_SLOT_SIZE) ", 0x90"

#define AGY_STUB_DECLARE(id, book) \
    static_assert(bk(#id) == (book), "symbols_gen.h is out of step with BOOKS"); \
    extern "C" __attribute__((naked, used, visibility("default"), \
                              aligned(AGY_SLOT_SIZE), section(AGY_TRAMP_SECTION))) \
    void agy_stub__##id()

/* ABIInternal: cgocall spills its own RAX/RBX arguments into our reserved two words. */
#define AGY_STUB_FULLCGO(id, book, resume_off, ...) \
    AGY_STUB_DECLARE(id, book) { \
        __asm__(AGY_STUB_ENTER(book) \
            "leaq   " TO_STRING(AGY_OFF_BLOCK) "(%rsp), %rbx\n\t" \
            "movq   %r12, %rax\n\t" \
            "call   *agy_go__CGOCALL@GOTPCREL(%rip)\n\t" \
            AGY_STUB_LEAVE(id, resume_off, __VA_ARGS__)); \
        __builtin_unreachable(); \
    }

/* ABI0: asmcgocall reads fn/arg from the stack; it does not need register copies. */
#define AGY_STUB_ASMCGO(id, book, resume_off, ...) \
    AGY_STUB_DECLARE(id, book) { \
        __asm__(AGY_STUB_ENTER(book) \
            "movq   %r12, " TO_STRING(AGY_OFF_FN) "(%rsp)\n\t" \
            "leaq   " TO_STRING(AGY_OFF_BLOCK) "(%rsp), %rax\n\t" \
            "movq   %rax, " TO_STRING(AGY_OFF_ARG) "(%rsp)\n\t" \
            "call   *agy_go__ASMCGOCALL@GOTPCREL(%rip)\n\t" \
            AGY_STUB_LEAVE(id, resume_off, __VA_ARGS__)); \
        __builtin_unreachable(); \
    }

/* One stub per installed row, in BOOKS order. Slot alignment makes them tile the section;
 * agy_init catches any stub that outgrows its slot. */
AGY_STUBS(AGY_STUB_FULLCGO, AGY_STUB_ASMCGO)
