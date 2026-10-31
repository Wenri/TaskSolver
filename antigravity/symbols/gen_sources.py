#!/usr/bin/env python3
"""Generate the two sources derived from the C hook table in `src/procdef.h`.

    gen_sources.py <symbols.json> <symbols_gen.h>   # the C++ header (build step; gitignored output)
    gen_sources.py --hooks [<hooks.py>]             # the Python mirror (checked in; `pixi run shim-hooks`)

`src/symbols_gen.h` bakes symbols.json + procdef.h into:
  1. `extern "C"` declarations for the Go symbols exported by libagy.so (`agy_go__*`)
     and the static trampoline stubs exported by `vendor/agy` (`agy_stub__<ID>`) so
     `ld` / `ld.so` bind both directions at link/load time via ELF relocations.
  2. Static assembly cgocall/asmcgocall trampoline stubs (`agy_stub__<ID>`) in
     `.section .agy_trampolines,"ax",@progbits` (256-byte slots bounded by
     `agy_tramp_region_start` .. `agy_tramp_region_end`), each statically bound via `ld`
     to its dedicated C handler (`agy_on_*`), `runtime.cgocall`/`runtime.asmcgocall`,
     displaced Go frame-setup prologue bytes, and `agy_go__*` resume target.
  3. `AGY_SYMS`, one row per procdef.h row keyed by that row's short id, and the `consteval`
     resolvers over it (`agy_fn`, `agy_stub`, `agy_skip`, `agy_ow`, `agy_slot`). The
     id -> Go symbol -> address join happens here, so no long Go symbol string reaches the C++.

`pyagy/agy_process/hooks.py` is the stdlib-pure mirror behind the public `pyagy.HOOKS`
introspection surface. It used to be maintained by hand and drifted three rows behind procdef.h,
so it is generated — but it is also checked in (it ships in the wheel and is imported inside the
CLI's embedded interpreter), which is why the build does not rewrite it. Run `pixi run shim-hooks`
after editing the C table.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agyelf import (active_rows, c_sym_name, export_map,  # noqa: E402
                    load_handlers, load_procdef)

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HOOKS_OUT = os.path.join(ROOT, "pyagy", "agy_process", "hooks.py")

SLOT_SIZE = 256
FRAME_SIZE = 120
FRAME_LO = 17
FRAME_HI = 182


# --- src/symbols_gen.h --------------------------------------------------------------------------
def emit_stub_asm(slot: int, row: dict, handler: str, c_sym: str, skip: int, phex: str) -> list[str]:
    hid = row["id"]
    mech = row["mech"]
    gw_sym = c_sym_name("ASMCGOCALL" if mech == "asmcgo" else "CGOCALL")
    ow = len(phex) // 2
    if ow not in (8, 11):
        raise SystemExit(f"hook {hid} ({row['symbol']}): unexpected prologue hex {phex!r} (ow={ow})")
    pbytes = ", ".join(f"0x{phex[j:j + 2]}" for j in range(0, len(phex), 2))
    resume_off = skip + ow
    enable_sym = "g_agy_conv_id" if hid == "FILE_OPEN" else "g_agy_enabled"

    return [
        f".globl agy_stub__{hid}",
        f".type agy_stub__{hid}, @function",
        f"agy_stub__{hid}:",
        f"    cmpb    $0, {enable_sym}(%rip)",
        f"    je      .Lpass__{slot}",
        f"    subq    ${FRAME_SIZE}, %rsp",
        f".Lframe_lo__{slot}:",
        "    movq    %rax, 24(%rsp)",
        "    movq    %rbx, 32(%rsp)",
        "    movq    %rcx, 40(%rsp)",
        "    movq    %rdi, 48(%rsp)",
        "    movq    %rsi, 56(%rsp)",
        "    movq    %r8,  64(%rsp)",
        "    movq    %r9,  72(%rsp)",
        "    movq    %r10, 80(%rsp)",
        "    movq    %r11, 88(%rsp)",
        "    movq    %rdx, 96(%rsp)",
        "    movq    %rbp, 104(%rsp)",
        f"    leaq    .Lkind__{slot}(%rip), %rax",
        "    movq    %rax, 16(%rsp)",
        "    xorl    %eax, %eax",
        "    movq    %rax, 112(%rsp)",
        "    leaq    16(%rsp), %rbx",
        f"    leaq    {handler}(%rip), %rax",
        "    movq    %rax, 0(%rsp)",
        "    movq    %rbx, 8(%rsp)",
        "    xorps   %xmm15, %xmm15",
        f"    call    *{gw_sym}@GOTPCREL(%rip)",
        "    movq    24(%rsp), %rax",
        "    movq    32(%rsp), %rbx",
        "    movq    40(%rsp), %rcx",
        "    movq    48(%rsp), %rdi",
        "    movq    56(%rsp), %rsi",
        "    movq    64(%rsp), %r8",
        "    movq    72(%rsp), %r9",
        "    movq    80(%rsp), %r10",
        "    movq    88(%rsp), %r11",
        "    movq    96(%rsp), %rdx",
        "    movq    104(%rsp), %rbp",
        "    movq    112(%rsp), %r12",
        f".Lframe_hi__{slot}:",
        f"    addq    ${FRAME_SIZE}, %rsp",
        "    testq   %r12, %r12",
        f"    jne     .Lret__{slot}",
        f".Lpass__{slot}:",
        f"    .byte   {pbytes}",
        f"    movq    {c_sym}@GOTPCREL(%rip), %r12",
        f"    leaq    {resume_off}(%r12), %r12",
        "    jmp     *%r12",
        f".Lret__{slot}:",
        "    xorps   %xmm15, %xmm15",
        "    ret",
        f"    .org agy_tramp_region_start + {(slot + 1) * SLOT_SIZE}, 0x90",
    ]


def gen_header(symbols_path: str, out_path: str) -> None:
    d = json.load(open(symbols_path))
    hooks = d["hooks"]
    skips = d.get("skips", {})
    prologues = d.get("prologues", {})
    all_rows = load_procdef()
    rows = active_rows(all_rows)
    slot_of = {r["id"]: i for i, r in enumerate(rows)}
    handlers, default_handler = load_handlers()
    exports = {hid: nm for hid, nm in export_map(all_rows).items() if nm in hooks}

    with open(out_path, "w") as f:
        w = f.write
        w("/* generated by gen_sources.py — do not edit */\n")
        w("#ifndef AGY_SYMBOLS_GEN_H\n#define AGY_SYMBOLS_GEN_H\n")
        w("#include <cstdint>\n#include <string_view>\n\n")
        w('inline constexpr char     AGY_BUILD_ID[]        = "%s";\n' % d.get("build_id", ""))
        w("inline constexpr uint64_t AGY_TEXT_BASE         = 0x%xULL;\n" % d.get("text_base", 0))
        w("inline constexpr uint64_t AGY_MODULEDATA_VADDR  = 0x%xULL;\n" % d.get("moduledata_vaddr", 0))
        w("inline constexpr uint32_t AGY_TRAMP_SLOT_SIZE   = %d;\n" % SLOT_SIZE)
        w("inline constexpr int      AGY_TRAMP_NSLOTS      = %d;\n" % len(rows))
        w("inline constexpr uint32_t AGY_TRAMP_FRAME       = %d;\n" % FRAME_SIZE)
        w("inline constexpr uint32_t AGY_TRAMP_FRAME_LO    = %d;\n" % FRAME_LO)
        w("inline constexpr uint32_t AGY_TRAMP_FRAME_HI    = %d;\n\n" % FRAME_HI)

        w('extern "C" {\n')
        for hid, nm in exports.items():
            w("void %s();   /* %s */\n" % (c_sym_name(hid), nm))
        for r in rows:
            w('__attribute__((visibility("default"))) void agy_stub__%s();\n' % r["id"])
        w("extern const uint8_t agy_tramp_region_start[];\n")
        w("extern const uint8_t agy_tramp_region_end[];\n")
        w("}\n\n")

        w("using agy_go_fn_t = void (*)();\n")
        w("/* One row per src/procdef.h row, keyed by the same short id hk() uses. The Go symbol\n")
        w(" * is resolved here, at generation time, so no long symbol string reaches the C++. */\n")
        w("struct agy_symrec {\n")
        w("    std::string_view id;\n")
        w("    agy_go_fn_t fn;     /* &agy_go__<symbol> */\n")
        w("    agy_go_fn_t stub;   /* &agy_stub__<id>, nullptr when the row is AGY_OFF */\n")
        w("    uint32_t skip;\n")
        w("    uint32_t ow;\n")
        w("    int slot;           /* index in agy_tramp_slots[], -1 when AGY_OFF */\n")
        w("};\n")
        w("inline constexpr agy_symrec AGY_SYMS[] = {\n")
        for r in all_rows:
            nm = r["symbol"]
            slot = slot_of.get(r["id"], -1)
            fn = "&" + c_sym_name(r["id"]) if nm in hooks else "nullptr"
            stub = f'&agy_stub__{r["id"]}' if slot >= 0 else "nullptr"
            w('    { "%s", %s, %s, %d, %d, %d },\n'
              % (r["id"], fn, stub, skips.get(nm, 0),
                 len(prologues.get(nm, "")) // 2, slot))
        w("};\n\n")

        w("consteval const agy_symrec *agy_row(std::string_view id) {\n")
        w("    for (auto &s : AGY_SYMS) if (s.id == id) return &s;\n")
        w("    return nullptr;\n}\n\n")
        w("consteval agy_go_fn_t agy_fn(std::string_view id) {\n")
        w("    auto r = agy_row(id); return r ? r->fn : nullptr;\n}\n\n")
        w("consteval agy_go_fn_t agy_stub(std::string_view id) {\n")
        w("    auto r = agy_row(id); return r ? r->stub : nullptr;\n}\n\n")
        w("consteval uint32_t agy_skip(std::string_view id) {\n")
        w("    auto r = agy_row(id); return r ? r->skip : 0;\n}\n\n")
        w("consteval uint32_t agy_ow(std::string_view id) {\n")
        w("    auto r = agy_row(id); return r ? r->ow : 0;\n}\n\n")
        w("consteval int agy_slot(std::string_view id) {\n")
        w("    auto r = agy_row(id); return r ? r->slot : -1;\n}\n\n")

        asm_lines = ['.pushsection .rodata.agy_trampolines,"a",@progbits']
        for slot, r in enumerate(rows):
            kind_esc = r["kind"].replace("\\", "\\\\").replace('"', '\\"')
            asm_lines.append(f'.Lkind__{slot}: .asciz "{kind_esc}"')
        asm_lines += [
            ".popsection",
            '.pushsection .agy_trampolines,"ax",@progbits',
            ".p2align 8",
            ".globl agy_tramp_region_start",
            ".hidden agy_tramp_region_start",
            "agy_tramp_region_start:",
        ]
        for slot, r in enumerate(rows):
            sym = r["symbol"]
            asm_lines.extend(emit_stub_asm(slot, r, handlers.get(r["kind"], default_handler),
                                           c_sym_name(r["id"]), skips[sym], prologues[sym]))
        asm_lines += [
            ".globl agy_tramp_region_end",
            ".hidden agy_tramp_region_end",
            "agy_tramp_region_end:",
            ".popsection",
        ]

        w("#ifdef AGY_EMIT_STATIC_TRAMPOLINES\n")
        w("__asm__(\n")
        for ln in asm_lines:
            esc_ln = ln.replace("\\", "\\\\").replace('"', '\\"')
            w(f'    "{esc_ln}\\n"\n')
        w(");\n")
        w("#endif /* AGY_EMIT_STATIC_TRAMPOLINES */\n\n")
        w("#endif\n")

    print("wrote %s (%d symbols, %d static trampoline stubs, build-id %s)"
          % (out_path, len(hooks), len(rows), d.get("build_id", "")))


# --- pyagy/agy_process/hooks.py -----------------------------------------------------------------
HOOKS_HEADER = '''"""Machine-readable mirror of the C hook table (src/procdef.h).

GENERATED — do not edit by hand. Regenerate with::

    pixi run shim-hooks

`pyagy.HOOKS` is a public introspection surface, so this file is checked in and ships in the
wheel. Stdlib-pure: agy_process imports it inside the instrumented CLI's embedded interpreter.

Each row mirrors one C row:
  id      short tag, the `hk("ID")` key at the C call sites
  symbol  the Go symbol the hook patches
  mode    "async" (log) | "sync" (block for a modify verdict)
  kind    the tag passed to dispatch(kind, stream_id, data)
  mech    "off" (not installed) | "fullcgo" | "asmcgo" — the cgocall trampoline flavour
  retcap  return-capture policy: 0 none, <0 special-cased, >0 min bytes. Every retcap != 0 hook
          is mech="off": the gum return-hook path it needed was retired and deleted, so this is
          the recorded register contract, not live behaviour.
"""

HOOKS = [
'''

HOOKS_FOOTER = ''']

#: kinds the Python layer DERIVES rather than receiving straight from a hook.
DERIVED_KINDS = ("genai_turn", "h2msg", "conversation_id", "callstack", "app_response")


def by_mech(mech):
    """Rows whose install mechanism is `mech` ("off" / "fullcgo" / "asmcgo")."""
    return [h for h in HOOKS if h["mech"] == mech]


def by_kind(kind):
    """Rows emitting `kind` (several hooks can share one kind)."""
    return [h for h in HOOKS if h["kind"] == kind]


def enabled_hooks():
    """Rows actually installed at runtime (everything not mech="off")."""
    return [h for h in HOOKS if h["mech"] != "off"]


def sync_capable():
    """Rows whose dispatch can return replacement bytes (mode="sync")."""
    return [h for h in HOOKS if h["mode"] == "sync"]
'''


def render_hooks(rows) -> str:
    out = [HOOKS_HEADER]
    for r in rows:
        out.append(
            '    {{"id": {id!r}, "symbol": {symbol!r},\n'
            '     "mode": {mode!r}, "kind": {kind!r}, "mech": {mech!r}, "retcap": {retcap!r}}},\n'
            .format(**r))
    out.append(HOOKS_FOOTER)
    return "".join(out)


def gen_hooks(out_path: str) -> None:
    rows = load_procdef()
    open(out_path, "w").write(render_hooks(rows))
    n_on = sum(1 for r in rows if r["mech"] != "off")
    print(f"wrote {out_path} ({len(rows)} hooks, {n_on} installed)")


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--hooks":
        if len(argv) > 2:
            raise SystemExit("usage: gen_sources.py --hooks [<hooks.py>]")
        gen_hooks(argv[1] if len(argv) == 2 else HOOKS_OUT)
        return
    if len(argv) != 2:
        raise SystemExit("usage: gen_sources.py <symbols.json> <symbols_gen.h>\n"
                         "       gen_sources.py --hooks [<hooks.py>]")
    gen_header(argv[0], argv[1])


if __name__ == "__main__":
    main()
