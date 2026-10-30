#!/usr/bin/env python3
"""Convert Google's PIE `agy` (Antigravity) Go executable into `libagy.so`.

Why this works without an external `agy_tls.S` reservation:
  - `agy` is an `ET_DYN` Position-Independent Executable whose Go runtime
    (`%fs:-0x3f0` for `g`) and embedded C++/TCMalloc runtime (`%fs:-0x8`, etc.)
    use Local-Exec negative offsets from `%fs:0` in `[%fs:-0x4a0 .. %fs:0)`.
  - Keeping `PT_TLS` inside `libagy.so`, clearing `DF_1_PIE` in `DT_FLAGS_1`,
    setting `DT_SONAME = "libagy.so"`, and exporting `agy_rt0_go` (`runtime.rt0_go`),
    `agy_firstmoduledata` (`runtime.firstmoduledata`), and all hooked Go target
    functions (`agy_go__*`) lets `gcc`/`g++` link a small C++ host executable
    (`vendor/agy`) against `-lagy` as its first `DT_NEEDED` dependency and bind
    every Go symbol + `firstmoduledata` directly via `ld`.
  - Because the host executable itself defines no thread-local variables (no
    `PT_TLS` segment of its own), glibc's `_dl_determine_tlsoffset` assigns the
    first static TLS slot `[%fs:-0x4a0 .. %fs:0)` directly to `libagy.so`.

Usage:  python3 make_libagy.py <in-agy-binary> <out-libagy.so>
"""
import os
import shutil
import sys

import lief

from build_symbols import (
    ELF,
    NOSPLIT_TARGETS,
    PROC_TARGETS,
    R_X86_64_RELATIVE,
    c_sym_name,
    find_pclntab,
)


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


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: make_libagy.py <in-agy-binary> <out-libagy.so>")
    in_path, out_path = sys.argv[1], sys.argv[2]
    data = open(in_path, "rb").read()
    bin_ = lief.parse(in_path)
    if bin_ is None:
        raise SystemExit(f"lief failed to parse {in_path}")

    pie_flag = int(lief.ELF.DynamicEntryFlags.FLAG.PIE)
    flags1 = bin_.get(lief.ELF.DynamicEntry.TAG.FLAGS_1)
    already_converted = (
        bin_.has_symbol("agy_rt0_go")
        and bin_.has_symbol("agy_firstmoduledata")
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

    n_exported = 0
    for nm in PROC_TARGETS + NOSPLIT_TARGETS:
        if nm in funcs:
            bin_.add_exported_function(funcs[nm], c_sym_name(nm))
            n_exported += 1

    if not bin_.has(lief.ELF.DynamicEntry.TAG.SONAME):
        bin_.add(lief.ELF.DynamicSharedObject("libagy.so"))

    tmp_path = f"{out_path}.tmp.{os.getpid()}"
    bin_.write(tmp_path)
    os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, out_path)
    print(
        f"wrote {out_path} (agy_rt0_go={funcs['runtime.rt0_go']:#x}, "
        f"agy_firstmoduledata={md_vaddr:#x}, {n_exported} Go hook symbols exported)"
    )


if __name__ == "__main__":
    main()
