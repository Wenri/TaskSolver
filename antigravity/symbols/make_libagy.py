#!/usr/bin/env python3
"""Convert Google's PIE `agy` (Antigravity) Go executable into `libagy.so` with
bidirectional static `ld` binding:
  - Exports `agy_rt0_go` (`runtime.rt0_go`), `agy_firstmoduledata` (`runtime.firstmoduledata`),
    and all 41 Go hook target functions (`agy_go__*`) in `libagy.so`'s `.dynsym`.
  - Imports each active static trampoline stub (`agy_stub__<ID>`, defined in `vendor/agy`'s
    `.agy_trampolines` section) via `R_X86_64_GLOB_DAT` relocations in `agy_tramp_slots[]`
    and statically patches each hooked Go function's `entry + skip` prologue in `libagy.so`
    with `jmp *agy_tramp_slots[slot](%rip)` (`FF 25 <disp32>`), so `ld.so` binds both
    directions at load time with zero runtime code patching or `mprotect`.

Usage:  python3 make_libagy.py <in-agy-binary> <out-libagy.so>
"""
import os
import shutil
import struct
import sys

import lief

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_symbols import (  # noqa: E402
    ELF,
    NOSPLIT_TARGETS,
    PROC_TARGETS,
    R_X86_64_RELATIVE,
    c_sym_name,
    find_pclntab,
)
from gen_hooks_py import parse as parse_procdef  # noqa: E402


def prologue_skip(elf: ELF, data: bytes, addr: int) -> int:
    try:
        b = data[elf.v2o(addr):elf.v2o(addr) + 48]
    except ValueError:
        return 0
    for k in range(40):
        if (b[k] & 0xFB) == 0x49 and b[k + 1] == 0x3B and (b[k + 2] & 0xC7) == 0x46 and b[k + 3] == 0x10:
            j = k + 4
            if b[j] == 0x0F and b[j + 1] in (0x82, 0x83, 0x86, 0x87):
                return j + 6
            if b[j] in (0x72, 0x73, 0x76, 0x77):
                return j + 2
            return j
    return 0


def resolve_exports(data: bytes) -> tuple[int, dict[str, int]]:
    elf = ELF(data)
    B = find_pclntab(data)
    u32, i32, u64 = elf.u32, elf.i32, elf.u64
    nfunc = u64(B + 8)
    funcnametab = u64(B + 32)
    pcln_off = u64(B + 64)
    FT = B + pcln_off
    FUNCBASE = B + pcln_off

    rela = elf.sec(".rela.dyn")
    if not rela:
        raise SystemExit("no .rela.dyn in agy binary")
    md_vaddr = None
    rbase, rcnt = rela["off"], rela["size"] // 24
    for i in range(rcnt):
        o = rbase + i * 24
        if u64(o + 16) == B and (u64(o + 8) & 0xFFFFFFFF) == R_X86_64_RELATIVE:
            md_vaddr = u64(o)
            break
    if md_vaddr is None:
        raise SystemExit("could not find moduledata in agy binary")

    relwin = {}
    for i in range(rcnt):
        o = rbase + i * 24
        roff = u64(o)
        if md_vaddr <= roff < md_vaddr + 0x400 and (u64(o + 8) & 0xFFFFFFFF) == R_X86_64_RELATIVE:
            relwin[roff] = u64(o + 16)
    text_base = relwin.get(md_vaddr + 0xB0)
    text_sec = elf.sec(".text")
    if text_base is None or not (text_sec["addr"] <= text_base < text_sec["addr"] + 0x1000):
        cands = sorted({v for v in relwin.values()
                        if text_sec["addr"] <= v < text_sec["addr"] + 0x1000})
        text_base = cands[0] if cands else text_sec["addr"]

    wanted = set(PROC_TARGETS) | set(NOSPLIT_TARGETS) | {"runtime.rt0_go"}
    found: dict[str, int] = {}
    for i in range(nfunc):
        eo = u32(FT + i * 8)
        fo = u32(FT + i * 8 + 4)
        noff = i32(FUNCBASE + fo + 4)
        o = B + funcnametab + noff
        nm = data[o:data.index(b"\0", o)].decode("utf-8", "replace")
        if nm in wanted and nm not in found:
            found[nm] = text_base + eo
    if "runtime.rt0_go" not in found:
        raise SystemExit("runtime.rt0_go not found in .gopclntab")
    return md_vaddr, found


def patch_static_hooks(elf_path: str, active_rows: list[dict]) -> int:
    """Statically patch each active hooked Go function in `elf_path` at `entry + skip`
    with `FF 25 <disp32>` (`jmp *agy_tramp_slots[slot](%rip)`) + 1-byte marker (`0x81`/`0x83`)
    at byte 6 so `ld.so` routes every call directly to `agy_stub__<ID>` in `vendor/agy`."""
    post_bin = lief.parse(elf_path)
    if post_bin is None:
        raise SystemExit(f"lief failed to re-parse {elf_path}")
    slots_sym = post_bin.get_symbol("agy_tramp_slots")
    if slots_sym is None or slots_sym.value == 0:
        raise SystemExit("agy_tramp_slots symbol missing in written libagy.so")
    slots_base = slots_sym.value

    with open(elf_path, "r+b") as f:
        raw = bytearray(f.read())
        elf = ELF(raw)
        n_patched = 0
        for slot, r in enumerate(active_rows):
            sym_name = c_sym_name(r["symbol"])
            sym = post_bin.get_symbol(sym_name)
            if sym is None or sym.value == 0:
                raise SystemExit(f"missing symbol {sym_name} in written libagy.so")
            fn_vaddr = sym.value
            skip = prologue_skip(elf, raw, fn_vaddr)
            if skip <= 0:
                raise SystemExit(f"active hook {r['id']} ({r['symbol']}) has skip={skip}")
            hook_vaddr = fn_vaddr + skip
            hook_off = elf.v2o(hook_vaddr)
            p = raw[hook_off:hook_off + 11]
            if p[:7] == b"\x55\x48\x89\xe5\x48\x81\xec":
                marker = 0x81
            elif p[:7] == b"\x55\x48\x89\xe5\x48\x83\xec":
                marker = 0x83
            elif p[:2] == b"\xff\x25" and p[6] in (0x81, 0x83):
                marker = p[6]
            else:
                raise SystemExit(
                    f"unexpected prologue at {r['symbol']}+{skip} ({hook_vaddr:#x}): {bytes(p).hex()}"
                )
            slot_vaddr = slots_base + slot * 8
            disp32 = slot_vaddr - (hook_vaddr + 6)
            patch = b"\xff\x25" + struct.pack("<i", disp32) + bytes([marker])
            f.seek(hook_off)
            f.write(patch)
            n_patched += 1
    return n_patched


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: make_libagy.py <in-agy-binary> <out-libagy.so>")
    in_path, out_path = sys.argv[1], sys.argv[2]
    procdef_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "procdef.h")
    rows = parse_procdef(open(procdef_path).read())
    active_rows = [r for r in rows if r["mech"] in ("fullcgo", "asmcgo")]

    data = open(in_path, "rb").read()
    bin_ = lief.parse(in_path)
    if bin_ is None:
        raise SystemExit(f"lief failed to parse {in_path}")

    pie_flag = int(lief.ELF.DynamicEntryFlags.FLAG.PIE)
    flags1 = bin_.get(lief.ELF.DynamicEntry.TAG.FLAGS_1)
    already_converted = (
        bin_.has_symbol("agy_rt0_go")
        and bin_.has_symbol("agy_firstmoduledata")
        and bin_.has_symbol("agy_tramp_slots")
        and all(bin_.has_symbol(f"agy_stub__{r['id']}") for r in active_rows)
        and bin_.has(lief.ELF.DynamicEntry.TAG.SONAME)
        and (flags1 is None or (flags1.value & pie_flag) == 0)
    )
    if already_converted:
        if os.path.abspath(in_path) != os.path.abspath(out_path):
            shutil.copy2(in_path, out_path)
            os.chmod(out_path, 0o755)
        print(f"{out_path}: already converted to libagy.so")
        return

    md_vaddr, funcs = resolve_exports(data)
    if flags1 is not None:
        flags1.value &= ~pie_flag

    bin_.add_exported_function(funcs["runtime.rt0_go"], "agy_rt0_go")
    md_sym = bin_.add_exported_function(md_vaddr, "agy_firstmoduledata")
    md_sym.type = lief.ELF.Symbol.TYPE.OBJECT
    md_sym.size = 568

    bss = bin_.get_section(".bss")
    if bss is None:
        raise SystemExit("no .bss section in agy binary")
    slots_vaddr = (bss.virtual_address + bss.size + 63) & ~63
    slots_size = 512
    new_bss_end = slots_vaddr + slots_size
    for seg in bin_.segments:
        if (
            seg.type == lief.ELF.Segment.TYPE.LOAD
            and seg.virtual_address <= bss.virtual_address < seg.virtual_address + seg.virtual_size
        ):
            if new_bss_end > seg.virtual_address + seg.virtual_size:
                seg.virtual_size = new_bss_end - seg.virtual_address
            break
    bss.size = new_bss_end - bss.virtual_address
    slots_sym = bin_.add_exported_function(slots_vaddr, "agy_tramp_slots")
    slots_sym.type = lief.ELF.Symbol.TYPE.OBJECT
    slots_sym.size = slots_size

    for slot, r in enumerate(active_rows):
        stub_sym = lief.ELF.Symbol()
        stub_sym.name = f"agy_stub__{r['id']}"
        stub_sym.type = lief.ELF.Symbol.TYPE.FUNC
        stub_sym.binding = lief.ELF.Symbol.BINDING.GLOBAL
        stub_sym.visibility = lief.ELF.Symbol.Symbol.VISIBILITY.DEFAULT if hasattr(lief.ELF.Symbol, "Symbol") else lief.ELF.Symbol.VISIBILITY.DEFAULT
        stub_sym.shndx = 0
        stub_sym.value = 0
        stub_sym.size = 0
        stub_sym = bin_.add_dynamic_symbol(stub_sym)
        rel = lief.ELF.Relocation(
            slots_vaddr + slot * 8,
            lief.ELF.Relocation.TYPE.X86_64_GLOB_DAT,
            lief.ELF.Relocation.ENCODING.RELA,
        )
        rel.symbol = stub_sym
        rel.addend = 0
        bin_.add_dynamic_relocation(rel)

    n_exported = 0
    for nm in PROC_TARGETS + NOSPLIT_TARGETS:
        if nm in funcs:
            bin_.add_exported_function(funcs[nm], c_sym_name(nm))
            n_exported += 1

    if not bin_.has(lief.ELF.DynamicEntry.TAG.SONAME):
        bin_.add(lief.ELF.DynamicSharedObject("libagy.so"))

    tmp_path = f"{out_path}.tmp.{os.getpid()}"
    bin_.write(tmp_path)
    n_patched = patch_static_hooks(tmp_path, active_rows)
    os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, out_path)
    print(
        f"wrote {out_path} (agy_rt0_go={funcs['runtime.rt0_go']:#x}, "
        f"agy_firstmoduledata={md_vaddr:#x}, {n_exported} Go symbols exported, "
        f"{n_patched} static hook jumps + GLOB_DAT relocations bound)"
    )


if __name__ == "__main__":
    main()
