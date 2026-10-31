#!/usr/bin/env python3
"""Convert Google's PIE `agy` (Antigravity) Go executable into `libagy.so` with
bidirectional static `ld` binding:
  - Exports `agy_rt0_go` (`runtime.rt0_go`), `agy_firstmoduledata` (`runtime.firstmoduledata`),
    and every Go hook target (`agy_go__*`) in `libagy.so`'s `.dynsym`.
  - Imports each active static trampoline stub (`agy_stub__<ID>`, defined in `vendor/agy`'s
    `.agy_trampolines` section) via `R_X86_64_GLOB_DAT` relocations in `agy_tramp_slots[]`
    and statically patches each hooked Go function's `entry + skip` prologue in `libagy.so`
    with `jmp *agy_tramp_slots[slot](%rip)` (`FF 25 <disp32>`), so `ld.so` binds both
    directions at load time with zero runtime code patching or `mprotect`.

The hook list and the pclntab/prologue arithmetic come from `agyelf.py`, shared with
`build_symbols.py`: the offset this tool patches and the offset the generated stub resumes at
are computed by the same code.

Usage:
    python3 make_libagy.py <in-agy-binary> <out-libagy.so>
    python3 make_libagy.py --update [<out-libagy.so>]   # fetch the LATEST agy, convert, print pins
    python3 make_libagy.py --wsl1 [<agy>]               # only the WSL1 mmap-flag patch, in place
"""
import hashlib
import json
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import urllib.request

import lief

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agyelf import (  # noqa: E402
    ELF,
    ENTRY_TARGET,
    GoImage,
    active_rows,
    c_sym_name,
    export_map,
    hook_targets,
    load_procdef,
    prologue_skip,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(_HERE, ".."))          # antigravity/
DEFAULT_OUT = os.path.join(ROOT, "vendor", "libagy.so")

MANIFEST_BASE = os.environ.get(
    "AGY_MANIFEST_BASE",
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app")


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


def patch_static_hooks(elf_path: str, rows: list[dict]) -> int:
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
        raw = f.read()                 # read-only here; the patches go out through `f` itself
        elf = ELF(raw)
        n_patched = 0
        for slot, r in enumerate(rows):
            sym_name = c_sym_name(r["id"])
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


def convert(in_path: str, out_path: str) -> None:
    rows = load_procdef()
    rows_active = active_rows(rows)
    targets = hook_targets(rows)

    data = open(in_path, "rb").read()
    bin_ = lief.parse(data)
    if bin_ is None:
        raise SystemExit(f"lief failed to parse {in_path}")

    pie_flag = int(lief.ELF.DynamicEntryFlags.FLAG.PIE)
    flags1 = bin_.get(lief.ELF.DynamicEntry.TAG.FLAGS_1)
    already_converted = (
        bin_.has_symbol("agy_rt0_go")
        and bin_.has_symbol("agy_firstmoduledata")
        and bin_.has_symbol("agy_tramp_slots")
        and all(bin_.has_symbol(f"agy_stub__{r['id']}") for r in rows_active)
        and bin_.has(lief.ELF.DynamicEntry.TAG.SONAME)
        and (flags1 is None or (flags1.value & pie_flag) == 0)
    )
    if already_converted:
        if os.path.abspath(in_path) != os.path.abspath(out_path):
            shutil.copy2(in_path, out_path)
            os.chmod(out_path, 0o755)
        print(f"{out_path}: already converted to libagy.so")
        return

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
    if flags1 is not None:
        flags1.value &= ~pie_flag

    bin_.add_exported_function(funcs[ENTRY_TARGET], "agy_rt0_go")
    md_sym = bin_.add_exported_function(img.md_vaddr, "agy_firstmoduledata")
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

    for slot, r in enumerate(rows_active):
        stub_sym = lief.ELF.Symbol()
        stub_sym.name = f"agy_stub__{r['id']}"
        stub_sym.type = lief.ELF.Symbol.TYPE.FUNC
        stub_sym.binding = lief.ELF.Symbol.BINDING.GLOBAL
        stub_sym.visibility = (lief.ELF.Symbol.Symbol.VISIBILITY.DEFAULT
                               if hasattr(lief.ELF.Symbol, "Symbol")
                               else lief.ELF.Symbol.VISIBILITY.DEFAULT)
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

    exports = export_map(rows)
    missing = sorted({nm for nm in exports.values() if nm not in funcs})
    if missing:
        raise SystemExit(f"hook targets not found in .gopclntab: {missing}")
    for hid, nm in exports.items():
        bin_.add_exported_function(funcs[nm], c_sym_name(hid))
    n_exported = len(exports)

    if not bin_.has(lief.ELF.DynamicEntry.TAG.SONAME):
        bin_.add(lief.ELF.DynamicSharedObject("libagy.so"))

    tmp_path = f"{out_path}.tmp.{os.getpid()}"
    bin_.write(tmp_path)
    n_patched = patch_static_hooks(tmp_path, rows_active)
    os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, out_path)
    print(
        f"wrote {out_path} (agy_rt0_go={funcs[ENTRY_TARGET]:#x}, "
        f"agy_firstmoduledata={img.md_vaddr:#x}, {n_exported} Go symbols exported, "
        f"{n_patched} static hook jumps + GLOB_DAT relocations bound)"
    )


def update(out_path: str) -> None:
    """Fetch the LATEST agy from the official updater manifest, convert it, and print the pin
    values for antigravity/CMakeLists.txt. The build-id WILL change, so follow with
    `pixi run shim-symbols` and commit symbols.json + CMakeLists.txt together. Reproducible
    builds use the pinned AGY_URL in CMakeLists.txt; this is the deliberate bump."""
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine, os.uname().machine)
    url = f"{MANIFEST_BASE}/manifests/linux_{arch}.json"
    print(f"[agy-update] querying {url}")
    with urllib.request.urlopen(url, timeout=60) as r:          # noqa: S310 (fixed https host)
        man = json.load(r)
    ver, tgz_url, sha = man["version"], man["url"], man["sha512"]
    print(f"[agy-update] latest = {ver}")

    with tempfile.TemporaryDirectory(prefix="agy-update-") as td:
        tgz = os.path.join(td, "agy.tar.gz")
        with urllib.request.urlopen(tgz_url, timeout=300) as r, open(tgz, "wb") as f:  # noqa: S310
            shutil.copyfileobj(r, f)
        digest = hashlib.sha512()
        with open(tgz, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha:
            raise SystemExit("[agy-update] FATAL: sha512 mismatch")
        with tarfile.open(tgz) as tar:
            member = tar.getmember("antigravity")
            tar.extract(member, td, filter="data")
        raw = os.path.join(td, "antigravity")
        os.chmod(raw, 0o755)
        convert(raw, out_path)          # applies the WSL1 patch itself when needed

    build_id = ""
    b = lief.parse(out_path)
    for note in getattr(b, "notes", []):
        if getattr(note, "type", None) == lief.ELF.Note.TYPE.GNU_BUILD_ID:
            build_id = bytes(note.description).hex()
    print(f"[agy-update] vendored libagy.so {ver} ({build_id}). Update the pins in CMakeLists.txt:")
    print(f'    set(AGY_VERSION "{ver}")')
    print(f'    set(AGY_URL     "{tgz_url}")')
    print(f'    set(AGY_SHA512  "{sha}")')
    print("[agy-update] then: pixi run shim-symbols   (re-resolve + commit symbols.json + CMakeLists.txt)")


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--update":
        if len(argv) > 2:
            raise SystemExit("usage: make_libagy.py --update [<out-libagy.so>]")
        update(argv[1] if len(argv) == 2 else DEFAULT_OUT)
        return
    if argv and argv[0] == "--wsl1":
        if len(argv) > 2:
            raise SystemExit("usage: make_libagy.py --wsl1 [<agy>]")
        patch_wsl1_file(os.path.expanduser(argv[1] if len(argv) == 2
                                           else "~/.local/bin/agy"))
        return
    if len(argv) != 2:
        raise SystemExit("usage: make_libagy.py <in-agy-binary> <out-libagy.so>\n"
                         "       make_libagy.py --update [<out-libagy.so>]\n"
                         "       make_libagy.py --wsl1 [<agy>]")
    convert(argv[0], argv[1])


if __name__ == "__main__":
    main()
