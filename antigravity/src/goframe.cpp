/* Go→C trampolines instantiated directly from BOOKS.
 *
 * Each naked template emits basic asm built at compile time. The exported
 * agy_stub__<id> label shares its entry address; ld.so binds libagy.so's eager
 * PLT slots to these labels without a wrapper call or jump. Hidden frame/end
 * labels provide each function's Go unwind metadata.
 *
 * Keep this translation unit out of LTO: the compiler cannot see the symbols
 * defined inside basic asm. __builtin_unreachable() suppresses a trailing UD2.
 */
#include "goframe.h"
#include "gocall.h"
#include "procdef.h"
#include "symbols_gen.h"
#include <algorithm>
#include <array>
#include <charconv>
#include <string>
#include <utility>

#ifndef __GXX_CONSTEXPR_ASM__
#error "agy stubs require GCC 15 or newer with constexpr asm strings"
#endif

static_assert(sizeof(agy_book_fn) == 8, "stub table indexing requires 8-byte handler pointers");
static_assert(std::size(BOOKS) <= INT32_MAX, "movq's signed immediate must zero the adjacent action");
static_assert(std::size(AGY_GO_FRAMES) == std::size(BOOKS), "one frame size per BOOKS row");
static_assert([] {
    for (size_t i = 0; i < std::size(BOOKS); ++i)
        if ((BOOKS[i].mech == AGY_OFF) != (AGY_GO_FRAMES[i] < 0)) return false;
    return true;
}(), "symbols_gen.h is out of step with BOOKS");

static constexpr auto STUB_BOOKS = [] {
    std::array<size_t, BK_STUB_COUNT> indices{};
    size_t count = 0;
    for (size_t i = 0; i < std::size(BOOKS); ++i)
        if (BOOKS[i].mech != AGY_OFF) indices[count++] = i;
    return indices;
}();

/* One register list supplies both halves of the snapshot. RBP is the last word
 * in the frame, as required by Go's unwinder. R12 is Go's scratch register. */
static constexpr struct {
    std::string_view name;
    unsigned offset;
} saved_regs[] = {
    {"rax", AGY_OFF_RAX}, {"rbx", AGY_OFF_RBX}, {"rcx", AGY_OFF_RCX},
    {"rdi", AGY_OFF_RDI}, {"rsi", AGY_OFF_RSI}, {"r8", AGY_OFF_R8},
    {"r9", AGY_OFF_R9}, {"r10", AGY_OFF_R10}, {"r11", AGY_OFF_R11},
    {"rdx", AGY_OFF_RDX}, {"rbp", AGY_OFF_RBP},
};

static constexpr std::string decimal(unsigned value)
{
    char buffer[10];
    auto [end, error] = std::to_chars(std::begin(buffer), std::end(buffer), value);
    if (error != std::errc{}) throw "assembly immediate exceeds decimal buffer";
    return {buffer, end};
}

static consteval std::string stub_asm(size_t index)
{
    const auto &book = BOOKS[index];
    if (book.mech == AGY_OFF) throw "disabled hook has no stub";

    const std::string id(book.id), entry = "agy_stub__" + id;
    std::string code = ".globl " + entry + "\n.type " + entry + ", @function\n" + entry + ":\n";
    auto pc = [&](std::string_view part) {
        const std::string label = "agy_pc__" + id + "_" + std::string(part);
        code += ".globl " + label + "\n.hidden " + label + "\n" + label + ":\n";
    };
    auto stack = [](unsigned offset) { return decimal(offset) + "(%rsp)"; };

    code += "movq g_agy_on+" + decimal(8 * index) + "(%rip), %r12\n"
            "testq %r12, %r12\n"
            "je 1f\n"
            "subq $" + decimal(AGY_FRAME) + ", %rsp\n";
    pc("frame_start");
    for (const auto &r : saved_regs)
        code += "movq %" + std::string(r.name) + ", " + stack(r.offset) + "\n";
    // One qword store initializes book and the adjacent action=0.
    code += "movq $" + decimal(index) + ", " + stack(AGY_OFF_BOOK) + "\n";

    if (book.mech == AGY_FULLCGO) {
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

    // Go expects X15 to contain zero on both PASS and RETURN paths.
    code += "xorps %xmm15, %xmm15\n";
    for (const auto &r : saved_regs)
        code += "movq " + stack(r.offset) + ", %" + std::string(r.name) + "\n";
    // LEA releases the frame without changing CMP's verdict flags.
    code += "cmpl $0, " + stack(AGY_OFF_ACTION) + "\n"
            "leaq " + stack(AGY_FRAME) + ", %rsp\n";
    pc("frame_end");
    code += "jne 2f\n"
            "1:\n"
            "pushq %rbp\n"
            "movq %rsp, %rbp\n"
            "subq $" + decimal(AGY_GO_FRAMES[index]) + ", %rsp\n"
            "jmp *agy_resume__" + id + "@GOTPCREL(%rip)\n"
            "2:\n"
            "ret\n";
    pc("end");
    code += ".size " + entry + ", .-" + entry + "\n";
    return code;
}

/* The temporary string may allocate during constant evaluation; asm receives
 * a fixed array whose size is derived from the complete generated text. */
template<auto MakeText>
static consteval auto asm_text()
{
    std::array<char, MakeText().size()> text{};
    std::ranges::copy(MakeText(), text.begin());
    return text;
}

template<size_t I>
struct agy_stub {
    static const unsigned char frame_start, frame_end, end;

    [[gnu::naked, gnu::used]] static void entry()
    {
        __asm__((asm_text<[] { return stub_asm(I); }>()));
        __builtin_unreachable();
    }

    static constexpr agy_stub_info info{&entry, &frame_start, &frame_end, &end};
};

/* GCC requires literal asm names on declarations. Specialize the boundary members
 * before make_stubs instantiates their constexpr descriptors. */
#include "stub_labels_gen.h"

template<size_t... I>
static consteval auto make_stubs(std::index_sequence<I...>)
{
    return std::array{agy_stub<STUB_BOOKS[I]>::info...};
}

static constexpr auto agy_stubs = make_stubs(std::make_index_sequence<BK_STUB_COUNT>{});
static_assert(agy_stubs.size() <= AGY_GOMOD_MAX_FUNCS,
              "more hooks than the static synthetic-moduledata holds — bump AGY_GOMOD_MAX_FUNCS in gomod.h");

std::span<const agy_stub_info> agy_stub_descriptors() noexcept
{
    return agy_stubs;
}
