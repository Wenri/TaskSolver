#!/usr/bin/env python3
"""Convert Google's PIE `agy` (Antigravity) Go executable into `libagy.so` with
bidirectional static `ld` binding:
  - Exports `agy_rt0_go` (`runtime.rt0_go`), `agy_firstmoduledata` (`runtime.firstmoduledata`),
    every Go hook target (`agy_go__*`), and each active hook's continuation
    (`agy_resume__<id>` at entry + skip + displaced prologue length) in `libagy.so`'s `.dynsym`.
  - Imports each active static trampoline stub (`agy_stub__<id>`, defined in `vendor/agy`'s
    `agy_trampolines` section) via eagerly bound `R_X86_64_JUMP_SLOT` relocations in
    `.got.plt.agy`. Each hooked function's `entry + skip` gets a five-byte `jmp rel32`
    to its `.plt.agy` entry; INT3 fills the rest of the displaced prologue. The PLT
    entry jumps through its GOT slot. `ld.so` binds both
    directions at load time with zero runtime code patching or `mprotect`.

Capture symbols.json and funcmap.tsv.gz from the final-layout unpatched ELF before writing
any hook jumps. The original prologue bytes in that metadata generate the static stubs.
Every conversion starts from the original upstream binary; converted libraries are outputs.

Usage (from antigravity/):
    python3 -m symbols.make_libagy <raw-agy> <out-libagy.so> <out-symbols.json>
    python3 -m symbols.make_libagy --update                 # print latest release pins
    python3 -m symbols.make_libagy --wsl1 [<agy>]             # only the WSL1 mmap patch, in place
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import urllib.request

import lief

from .agyelf import (
    ELF,
    ENTRY_TARGET,
    GoImage,
    active_rows,
    c_sym_name,
    export_map,
    hook_targets,
    load_procdef,
)

from .build_symbols import resolve_symbols, write_symbols

MANIFEST_BASE = os.environ.get(
    "AGY_MANIFEST_BASE",
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app")

HOOK_PLT = ".plt.agy"
HOOK_GOT = ".got.plt.agy"
PLT_ENTRY_SIZE = 8                           # jmp *slot(%rip); two-byte nop


def add_hook_plt(bin_: lief.ELF.Binary, rows: list[dict]) -> None:
    """Reserve PLT code/GOT slots; ld.so resolves the JUMP_SLOT relocations eagerly.

    Add these after the original-address exports so LIEF can adjust every reference if
    inserting program headers moves the input image. Encode the jumps after its final write.
    """
    plt = lief.ELF.Section(HOOK_PLT)
    plt.flags = lief.ELF.Section.FLAGS.ALLOC | lief.ELF.Section.FLAGS.EXECINSTR
    plt.alignment = PLT_ENTRY_SIZE
    plt.entry_size = PLT_ENTRY_SIZE
    plt.content = [0xcc] * (len(rows) * PLT_ENTRY_SIZE)
    bin_.add(plt)

    got = lief.ELF.Section(HOOK_GOT)
    got.flags = lief.ELF.Section.FLAGS.ALLOC | lief.ELF.Section.FLAGS.WRITE
    got.alignment = 8
    got.entry_size = 8
    got.content = [0] * (len(rows) * 8)
    got = bin_.add(got)
    if got is None or bin_.get_section(HOOK_PLT) is None:
        raise SystemExit("could not allocate hook PLT/GOT")

    for slot, row in enumerate(rows):
        symbol = lief.ELF.Symbol()
        symbol.name = f"agy_stub__{row['id']}"
        symbol.type = lief.ELF.Symbol.TYPE.FUNC
        symbol.binding = lief.ELF.Symbol.BINDING.GLOBAL
        symbol.shndx = 0
        symbol = bin_.add_dynamic_symbol(symbol)
        rel = lief.ELF.Relocation(
            got.virtual_address + slot * 8,
            lief.ELF.Relocation.TYPE.X86_64_JUMP_SLOT,
            lief.ELF.Relocation.ENCODING.RELA,
        )
        rel.symbol = symbol
        bin_.add_pltgot_relocation(rel)


def rel32(target: int, next_pc: int, context: str) -> bytes:
    """Fail conversion instead of publishing an out-of-range hook or PLT jump."""
    displacement = target - next_pc
    if not -(1 << 31) <= displacement < (1 << 31):
        raise SystemExit(f"{context}: displacement {displacement} exceeds signed 32-bit range")
    return struct.pack("<i", displacement)


def is_wsl1():
    """WSL1 needs the mmap patch below. `AGY_WSL1=0/1` overrides the kernel sniff."""
    if "AGY_WSL1" in os.environ:
        return os.environ["AGY_WSL1"] == "1"
    return "Microsoft" in os.uname().release


# agy bundles Google tcmalloc, which reserves its arenas with
# MAP_PRIVATE|MAP_ANONYMOUS|MAP_FIXED_NOREPLACE (0x100022). MAP_FIXED_NOREPLACE is Linux 4.17+;
# WSL1's 4.4 kernel emulation *rejects* it instead of ignoring it like a genuinely old kernel, so
# tcmalloc's MmapAligned() fails and agy dies with SIGABRT before main — every subcommand.
# Clearing the bit degrades the call to a plain hinted mmap (0x22), and WSL1 does honour high mmap
# hints, so tcmalloc gets its tagged region. Same length, so addresses and the build-id survive.
WSL1_MMAP_FLAGS = 0x100022
_MOV_R32_IMM32 = frozenset(range(0xB8, 0xC0))   # b8..bf: mov eXX/r8d-r15d, imm32


def patch_wsl1(data: bytearray) -> int:
    """Clear MAP_FIXED_NOREPLACE in every `mov r32, 0x100022` in `data`; returns the site count.

    Only real instructions are touched — the immediate must follow a b8..bf opcode byte. The same
    four bytes occur ~100 times in agy's data tables, and patching those would corrupt it.
    """
    imm = struct.pack("<I", WSL1_MMAP_FLAGS)
    sites, i = [], data.find(imm)
    while i != -1:
        if data[i - 1] in _MOV_R32_IMM32:
            sites.append(i + 2)                 # the 0x10 byte (bit 0x100000) of the LE immediate
        i = data.find(imm, i + 1)
    bad = [s for s in sites if data[s] != 0x10]
    if bad:
        raise SystemExit(f"[wsl1] abort: {len(bad)} site(s) not 0x10 where expected; agy layout changed")
    if not sites:
        raise SystemExit("[wsl1] no `mov r32, 0x100022` sites found — already patched, or agy changed")
    for s in sites:
        data[s] = 0x00
    return len(sites)


def patch_wsl1_file(path: str) -> None:
    """Apply `patch_wsl1` to `path` in place, keeping a one-time `.orig` backup.

    For patching an agy you run directly (`--wsl1 ~/.local/bin/agy`); re-run after each upgrade,
    since the installer redownloads the binary. WSL1 also needs agy launched through the system
    loader (WSL1 issue #38788):

        agy() { /lib64/ld-linux-x86-64.so.2 "$HOME/.local/bin/agy" "$@"; }

    None of this applies to the vendored libagy.so, which `convert()` patches on its own.
    """
    data = bytearray(open(path, "rb").read())
    n = patch_wsl1(data)
    bak = path + ".orig"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print(f"[wsl1] backed up original -> {bak}")
    open(path, "wb").write(bytes(data))
    os.chmod(path, 0o755)
    print(f"[wsl1] cleared MAP_FIXED_NOREPLACE at {n} site(s) in {path}")


def patch_static_hooks(elf_path: str, rows: list[dict], metadata: dict) -> int:
    """Write PLT entries, five-byte hooks and INT3 tails after validating the unpatched ELF."""
    with open(elf_path, "r+b") as f:
        raw = f.read()
        elf = ELF(raw)
        plt, got = (elf.bin.get_section(name) for name in (HOOK_PLT, HOOK_GOT))
        if (plt is None or got is None or plt.size < len(rows) * PLT_ENTRY_SIZE
                or got.size < len(rows) * 8):
            raise SystemExit("hook PLT/GOT missing or too small in written libagy.so")
        flags = elf.bin.get(lief.ELF.DynamicEntry.TAG.FLAGS)
        flags1 = elf.bin.get(lief.ELF.DynamicEntry.TAG.FLAGS_1)
        if not (elf.bin.has(lief.ELF.DynamicEntry.TAG.BIND_NOW)
                or (flags is not None and flags.value & 8)
                or (flags1 is not None and flags1.value & 1)):
            raise SystemExit("hook PLT requires eager binding before Go starts")
        relocations = {r.address: r for r in elf.bin.pltgot_relocations}
        patches = []
        for slot, row in enumerate(rows):
            name = row["symbol"]
            entry = metadata["hooks"][name]
            skip = metadata["skips"][name]
            prologue = bytes.fromhex(metadata["prologues"][name])
            hook = entry + skip
            offset = elf.v2o(hook)
            if skip <= 0 or len(prologue) not in (8, 11) or raw[offset:offset + len(prologue)] != prologue:
                raise SystemExit(f"original prologue does not match metadata for {row['id']}")
            resume = elf.bin.get_symbol(f"agy_resume__{row['id']}")
            if resume is None or resume.value != hook + len(prologue):
                raise SystemExit(f"continuation export does not match metadata for {row['id']}")
            plt_pc = plt.virtual_address + slot * PLT_ENTRY_SIZE
            got_pc = got.virtual_address + slot * 8
            relocation = relocations.get(got_pc)
            if (relocation is None or relocation.type != lief.ELF.Relocation.TYPE.X86_64_JUMP_SLOT
                    or not relocation.has_symbol or relocation.symbol.name != f"agy_stub__{row['id']}"):
                raise SystemExit(f"PLT binding does not match hook {row['id']}")
            plt_offset = elf.v2o(plt_pc)
            if raw[plt_offset:plt_offset + PLT_ENTRY_SIZE] != b"\xcc" * PLT_ENTRY_SIZE:
                raise SystemExit(f"PLT entry is not unpatched for {row['id']}")
            patches.append((plt_offset, b"\xff\x25" + rel32(got_pc, plt_pc + 6,
                                                          f"PLT {row['id']}") + b"\x66\x90"))
            # The stub replays the full saved prologue and resumes after it; trap on
            # accidental entry into the unreachable bytes left behind by the jump.
            jump = b"\xe9" + rel32(plt_pc, hook + 5, f"hook {row['id']}")
            patches.append((offset, jump + b"\xcc" * (len(prologue) - len(jump))))
        for offset, patch in patches:
            f.seek(offset)
            f.write(patch)
    return len(rows)


def convert(in_path: str, out_path: str, symbols_path: str) -> None:
    funcmap_path = os.path.join(os.path.dirname(symbols_path), "funcmap.tsv.gz")
    paths = (in_path, out_path, symbols_path, funcmap_path)
    if len({os.path.realpath(p) for p in paths}) != len(paths):
        raise SystemExit("raw input, library, symbols JSON and funcmap must be distinct files")
    rows = load_procdef()
    rows_active = active_rows(rows)
    targets = hook_targets(rows)

    data = open(in_path, "rb").read()
    bin_ = lief.parse(data)
    if bin_ is None:
        raise SystemExit(f"lief failed to parse {in_path}")

    if bin_.has_symbol("agy_rt0_go"):
        raise SystemExit("conversion requires the original upstream agy binary, not a converted library")

    # WSL1 cannot run the stock binary at all, so patch before converting: the fix is
    # length-preserving, so every address and the build-id survive into libagy.so.
    if is_wsl1():
        buf = bytearray(data)
        n_wsl1 = patch_wsl1(buf)
        data = bytes(buf)
        bin_ = lief.parse(data)
        if bin_ is None:
            raise SystemExit("lief failed to re-parse the WSL1-patched agy")
        print(f"[wsl1] cleared MAP_FIXED_NOREPLACE at {n_wsl1} site(s) before conversion")

    img = GoImage(data)
    funcs = img.resolve(list(targets) + [ENTRY_TARGET])
    if ENTRY_TARGET not in funcs:
        raise SystemExit(f"{ENTRY_TARGET} not found in .gopclntab")
    flags1 = bin_.get(lief.ELF.DynamicEntry.TAG.FLAGS_1)
    if flags1 is not None:
        flags1.remove(lief.ELF.DynamicEntryFlags.FLAG.PIE)
        flags1.add(lief.ELF.DynamicEntryFlags.FLAG.NOW)
    else:
        bin_.add(lief.ELF.DynamicEntry(lief.ELF.DynamicEntry.TAG.BIND_NOW, 0))

    bin_.add_exported_function(funcs[ENTRY_TARGET], "agy_rt0_go")
    md_sym = bin_.add_exported_function(img.md_vaddr, "agy_firstmoduledata")
    md_sym.type = lief.ELF.Symbol.TYPE.OBJECT
    md_sym.size = 568

    exports = export_map(rows)
    missing = sorted({nm for nm in exports.values() if nm not in funcs})
    if missing:
        raise SystemExit(f"hook targets not found in .gopclntab: {missing}")
    for hid, nm in exports.items():
        bin_.add_exported_function(funcs[nm], c_sym_name(hid))
    n_exported = len(exports)
    for row in rows_active:
        entry = funcs[row["symbol"]]
        skip = img.prologue_skip(entry)
        prologue = img.frame_prologue_hex(entry, skip)
        if skip <= 0 or len(prologue) not in (16, 22):
            raise SystemExit(f"cannot resolve original prologue for {row['id']} ({row['symbol']})")
        bin_.add_exported_function(entry + skip + len(prologue) // 2, f"agy_resume__{row['id']}")

    add_hook_plt(bin_, rows_active)

    if not bin_.has(lief.ELF.DynamicEntry.TAG.SONAME):
        bin_.add(lief.ELF.DynamicSharedObject("libagy.so"))

    # LIEF finalizes the ELF layout once. Resolve all addresses and original bytes from
    # that unpatched image; no LIEF rewrite or prologue decoding occurs after hook patching.
    out_dir = os.path.dirname(os.path.abspath(out_path))
    symbols_dir = os.path.dirname(os.path.abspath(symbols_path))
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agy-convert-", dir=out_dir) as staging:
        staged_lib = os.path.join(staging, "libagy.so")
        staged_symbols = os.path.join(staging, "symbols.json")
        bin_.write(staged_lib)
        with open(staged_lib, "rb") as f:
            metadata, funcmap = resolve_symbols(f.read(), out_path, rows)
        write_symbols(metadata, funcmap, staged_symbols)
        n_patched = patch_static_hooks(staged_lib, rows_active, metadata)
        os.chmod(staged_lib, 0o755)

        # Validation and staging must finish before replacing any existing build output.
        os.makedirs(symbols_dir, exist_ok=True)
        os.replace(staged_symbols, symbols_path)
        os.replace(os.path.join(staging, "funcmap.tsv.gz"), funcmap_path)
        os.replace(staged_lib, out_path)
    print(f"wrote {out_path}, {symbols_path} and funcmap.tsv.gz "
          f"({n_exported} Go entry + {len(rows_active)} continuation exports, {n_patched} hook jumps)")


def update() -> None:
    """Print the latest release pins; the normal build fetches and verifies that release."""
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine, os.uname().machine)
    url = f"{MANIFEST_BASE}/manifests/linux_{arch}.json"
    print(f"[agy-update] querying {url}")
    with urllib.request.urlopen(url, timeout=60) as r:          # noqa: S310 (fixed https host)
        man = json.load(r)
    print(f"[agy-update] latest = {man['version']}. Update the pins in CMakeLists.txt:")
    print(f'    set(AGY_VERSION "{man["version"]}")')
    print(f'    set(AGY_URL     "{man["url"]}")')
    print(f'    set(AGY_SHA512  "{man["sha512"]}")')
    print("[agy-update] then: pixi run build-shim   (fetch, verify, convert and regenerate metadata)")


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--update":
        if len(argv) != 1:
            raise SystemExit("usage: python3 -m symbols.make_libagy --update")
        update()
        return
    if argv and argv[0] == "--wsl1":
        if len(argv) > 2:
            raise SystemExit("usage: python3 -m symbols.make_libagy --wsl1 [<agy>]")
        patch_wsl1_file(os.path.expanduser(argv[1] if len(argv) == 2
                                           else "~/.local/bin/agy"))
        return
    if len(argv) != 3:
        raise SystemExit("usage: python3 -m symbols.make_libagy <raw-agy> <out-libagy.so> <out-symbols.json>\n"
                         "       python3 -m symbols.make_libagy --update\n"
                         "       python3 -m symbols.make_libagy --wsl1 [<agy>]")
    convert(argv[0], argv[1], argv[2])


if __name__ == "__main__":
    main()
