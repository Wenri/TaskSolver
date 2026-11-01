/* Go→C trampolines instantiated directly from BOOKS.
 *
 * One naked template per stubbed row emits basic asm built at compile time. Its exported
 * agy_stub__<id> label shares the entry address, so ld.so binds libagy.so's eager PLT slots
 * straight to it; the local frame/end labels give gomod.cpp each function's unwind bounds.
 *
 * Keep out of LTO: the compiler cannot see symbols defined inside basic asm.
 * __builtin_unreachable() suppresses a trailing UD2.
 */
#include "goframe.h"
#include "gocall.h"
#include "procdef.h"
#include "symbols_gen.h"
#include <algorithm>
#include <array>
#include <charconv>
#include <string>

#ifndef __GXX_CONSTEXPR_ASM__
#error "agy stubs require GCC 15 or newer with constexpr asm strings"
#endif

static_assert(sizeof(agy_book_fn) == 8, "the stub's table load assumes 8-byte handler pointers");

/* Saved and restored in this order; RBP must be the frame's last word for Go's unwinder.
 * R12 is Go's scratch register. */
static constexpr struct {
    std::string_view name;
    unsigned offset;
} saved_regs[] = {
    {"rax", AGY_OFF_RAX}, {"rbx", AGY_OFF_RBX}, {"rcx", AGY_OFF_RCX},
    {"rdi", AGY_OFF_RDI}, {"rsi", AGY_OFF_RSI}, {"r8", AGY_OFF_R8},
    {"r9", AGY_OFF_R9}, {"r10", AGY_OFF_R10}, {"r11", AGY_OFF_R11},
    {"rdx", AGY_OFF_RDX}, {"rbp", AGY_OFF_RBP},
};

/* Ten digits hold every unsigned, so to_chars always succeeds here. */
static constexpr std::string decimal(unsigned value)
{
    char buffer[10];
    return {buffer, std::to_chars(buffer, std::end(buffer), value).ptr};
}

static consteval std::string stub_asm(int index, int go_frame)
{
    const auto &book = BOOKS[index];
    const std::string id(book.id), entry = "agy_stub__" + id;
    std::string code = ".globl " + entry + "\n.type " + entry + ", @function\n" + entry + ":\n";
    auto pc = [&](std::string_view part) {
        code += "agy_pc__" + id + "_" + std::string(part) + ":\n";
    };
    auto stack = [](unsigned offset) { return decimal(offset) + "(%rsp)"; };

    code += "movq g_agy_on+" + decimal(8 * index) + "(%rip), %r12\n"
            "testq %r12, %r12\n"
            "je 1f\n"
            "subq $" + decimal(AGY_FRAME) + ", %rsp\n";
    pc("frame_start");
    for (const auto &r : saved_regs)
        code += "movq %" + std::string(r.name) + ", " + stack(r.offset) + "\n";
    // One qword sets book and clears the adjacent action.
    code += "movq $" + decimal(index) + ", " + stack(AGY_OFF_BOOK) + "\n";

    if (book.mech == AGY_FULLGO) {
        // ABIInternal: cgocall spills its own fn/arg into our reserved two words.
        code += "leaq " + stack(AGY_OFF_BLOCK) + ", %rbx\n"
                "movq %r12, %rax\n"
                "call *agy_go__CGOCALL@GOTPCREL(%rip)\n";
    } else {
        // ABI0: asmcgocall reads fn/arg from the stack.
        code += "movq %r12, " + stack(AGY_OFF_FN) + "\n"
                "leaq " + stack(AGY_OFF_BLOCK) + ", %rax\n"
                "movq %rax, " + stack(AGY_OFF_ARG) + "\n"
                "call *agy_go__ASMCGOCALL@GOTPCREL(%rip)\n";
    }

    // Go expects X15 zero on both paths.
    code += "xorps %xmm15, %xmm15\n";
    for (const auto &r : saved_regs)
        code += "movq " + stack(r.offset) + ", %" + std::string(r.name) + "\n";
    // LEA keeps CMP's flags.
    code += "cmpl $0, " + stack(AGY_OFF_ACTION) + "\n"
            "leaq " + stack(AGY_FRAME) + ", %rsp\n";
    pc("frame_end");
    code += "jne 2f\n"
            "1:\n"
            "pushq %rbp\n"
            "movq %rsp, %rbp\n"
            "subq $" + decimal(go_frame) + ", %rsp\n"
            "jmp *agy_resume__" + id + "@GOTPCREL(%rip)\n"
            "2:\n"
            "ret\n";
    pc("end");
    code += ".size " + entry + ", .-" + entry + "\n";
    return code;
}

/* stub_asm's string may allocate while constant-evaluating; asm needs a fixed array. */
template<int Book, int GoFrame>
static consteval auto stub_text()
{
    std::array<char, stub_asm(Book, GoFrame).size()> text{};
    std::ranges::copy(stub_asm(Book, GoFrame), text.begin());
    return text;
}

template<int Book, int GoFrame>
struct agy_stub {
    static const unsigned char frame_start, frame_end, end;

    [[gnu::naked, gnu::used]] static void entry()
    {
        __asm__((stub_text<Book, GoFrame>()));
        __builtin_unreachable();
    }

    static constexpr agy_stub_info info{&entry, &frame_start, &frame_end, &end};
};

/* bk() rejects an unknown id; this catches one BOOKS has since turned off. GCC also needs
 * literal asm names on declarations, so each row binds its own boundary members here. */
#define AGY_STUB_LABELS(id, go_frame)                                                        \
    static_assert(BOOKS[bk(id)].mech != AGY_OFF, "symbols_gen.h stubs a disabled row: " id); \
    template<> const unsigned char agy_stub<bk(id), go_frame>::frame_start                   \
        __asm__("agy_pc__" id "_frame_start");                                               \
    template<> const unsigned char agy_stub<bk(id), go_frame>::frame_end                     \
        __asm__("agy_pc__" id "_frame_end");                                                 \
    template<> const unsigned char agy_stub<bk(id), go_frame>::end                           \
        __asm__("agy_pc__" id "_end");
AGY_STUBS(AGY_STUB_LABELS)

/* goframe.h sizes this BK_STUB_COUNT, so a list missing a row fails to compile. */
#define AGY_STUB_INFO(id, go_frame) agy_stub<bk(id), go_frame>::info,
extern constexpr agy_stub_info agy_stubs[] = { AGY_STUBS(AGY_STUB_INFO) };

static_assert(std::size(agy_stubs) <= AGY_GOMOD_MAX_FUNCS,
              "more books than the static synthetic-moduledata holds — bump AGY_GOMOD_MAX_FUNCS in gomod.h");
