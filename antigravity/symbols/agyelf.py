#!/usr/bin/env python3
"""Shared parsing for the agy toolchain: the C hook table, ELF, and Go's pclntab.

`make_libagy.py` (raw agy -> libagy.so) and `build_symbols.py` (libagy.so -> symbols.json) both
have to find Go's moduledata, walk its function table and compute the same prologue skips. They
used to carry two copies of that code plus two copies of the hook-target list; a hook whose skip
differed between them would be patched at one offset and resumed at another. `gen_sources.py`
(procdef.h -> symbols_gen.h / hooks.py) is the third consumer. Everything any of them needs lives
here, so they cannot disagree.

Every fact about a hook is read from `src/procdef.h` — the targets, the install mechanism and the
handler each kind binds to — so that one C table stays the single source the host, the generated
assembly and the Python mirror are all built from. Nothing here is specific to one agy release.
"""
import os
import re
import struct

lief = None


def _load_lief():
    """Import lief on first ELF use, and only then.

    The procdef/codegen half of this module parses C text, so `gen_sources.py` must stay runnable
    under a plain interpreter; only the binary-reading half needs lief.
    """
    global lief
    if lief is None:
        import lief as _lief
        _lief.logging.disable()   # agy's layout makes lief chatty; the parse itself is fine
        lief = _lief


#: The one hook table. Every tool here reads it from this path unless given another.
PROCDEF_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "procdef.h"))

#: Go runtime entry points the static trampolines *call* (never hook): each stub hands its frame
#: to the cgocall gateway. They carry synthetic ids so they are exported and referenced exactly
#: like hook rows; the generated stub assembly calls `agy_go__CGOCALL` / `agy_go__ASMCGOCALL`.
GATEWAYS = {"CGOCALL": "runtime.cgocall", "ASMCGOCALL": "runtime.asmcgocall"}
GATEWAY_TARGETS = tuple(GATEWAYS.values())

#: Exported so the C++ host can start the Go runtime itself (antigravity.cpp calls it last).
ENTRY_TARGET = "runtime.rt0_go"


def c_sym_name(hook_id: str) -> str:
    """The C/ELF identifier libagy.so exports a hook's Go function under.

    Keyed by the row's short id, not by the Go symbol: ids are already valid C identifiers, and
    it makes every synthetic name in the pair of binaries read the same way — `agy_go__<ID>` is
    the Go side of exactly the row whose host side is `agy_stub__<ID>`. The Go symbol a row
    points at lives in procdef.h and symbols.json, which is where it is resolved.

    One Go function reached by two rows is exported once per id, as aliases on one address.
    """
    return "agy_go__" + hook_id


def export_map(rows):
    """`{id: Go symbol}` for every name libagy.so must export as `agy_go__<id>`.

    One entry per procdef row (so a disabled row keeps its export, and a function named by two
    rows is aliased under both ids), plus the cgocall gateways under their synthetic ids.
    """
    out = {r["id"]: r["symbol"] for r in rows}
    clash = set(out) & set(GATEWAYS)
    if clash:
        raise SystemExit(f"procdef hook id(s) collide with the gateway ids: {sorted(clash)}")
    out.update(GATEWAYS)
    return out


# --- src/procdef.h ------------------------------------------------------------------------------
# A row: { "ID", <symbol>, WIRE_x, "kind", AGY_x, retcap },
# <symbol> is either a quoted literal or a MACRO "suffix" concatenation (e.g. CAC "StreamGenerate").
_ROW = re.compile(
    r'\{\s*"(?P<id>[A-Z0-9_]+)"\s*,\s*'
    r'(?P<sym>(?:[A-Z_][A-Z0-9_]*\s*)?"(?:[^"\\]|\\.)*")\s*,\s*'
    r'(?P<mode>WIRE_\w+)\s*,\s*'
    r'"(?P<kind>[^"]*)"\s*,\s*'
    r'(?P<mech>AGY_\w+)\s*,\s*'
    r'(?P<retcap>-?\d+)\s*\}'
)
_MACRO_DEF = re.compile(r'^\s*#define\s+([A-Z_][A-Z0-9_]*)\s+("(?:[^"\\]|\\.)*")\s*$', re.M)


def _unquote(lit):
    return re.sub(r'\\(.)', r'\1', lit[1:-1])


def parse_procdef(src_text):
    """Every hook row from procdef.h, in table order."""
    macros = {name: _unquote(val) for name, val in _MACRO_DEF.findall(src_text)}
    rows = []
    for m in _ROW.finditer(src_text):
        sym = m.group("sym").strip()
        prefix = ""
        mac = re.match(r'^([A-Z_][A-Z0-9_]*)\s*(".*")$', sym, re.S)
        if mac:
            prefix = macros.get(mac.group(1), "")
            sym = mac.group(2)
        rows.append({
            "id": m.group("id"),
            "symbol": prefix + _unquote(sym),
            "mode": "sync" if m.group("mode") == "WIRE_SYNC" else "async",
            "kind": m.group("kind"),
            "mech": {"AGY_OFF": "off", "AGY_FULLCGO": "fullcgo",
                     "AGY_ASMCGO": "asmcgo"}.get(m.group("mech"), m.group("mech").lower()),
            "retcap": int(m.group("retcap")),
        })
    return rows


def load_procdef(path=None):
    """The hook rows from procdef.h. Empty means the table or this parser broke — never a
    silently hook-free build, so it raises."""
    path = path or PROCDEF_PATH
    with open(path) as f:
        rows = parse_procdef(f.read())
    if not rows:
        raise SystemExit(f"no hook rows parsed from {path}")
    return rows


# `consteval agy_hook_fn agy_handler_for(kind)`: `if (kind == "x") return &agy_on_x;` … `return &d;`
_HANDLER_FN = re.compile(
    r'consteval\s+agy_hook_fn\s+agy_handler_for\s*\([^)]*\)\s*\{(.*?)\n\}', re.S)
_HANDLER_CASE = re.compile(r'kind\s*==\s*"([^"]*)"\s*\)\s*return\s*&\s*(\w+)\s*;')
_HANDLER_DEFAULT = re.compile(r'^\s*return\s*&\s*(\w+)\s*;', re.M)


def load_handlers(path=None):
    """`({kind: C handler}, default handler)` parsed from procdef.h's `agy_handler_for`.

    The generated trampoline stubs call the handler directly, and the C `agy_hook` rows carry the
    same pointer in `.handler`. Parsing the C function keeps those two from drifting: a kind given
    a dedicated handler in procdef.h gets it in the assembly too, with no second table to update.
    """
    path = path or PROCDEF_PATH
    with open(path) as f:
        src = f.read()
    m = _HANDLER_FN.search(src)
    if not m:
        raise SystemExit(f"could not find agy_handler_for() in {path}")
    body = m.group(1)
    default = _HANDLER_DEFAULT.findall(body)   # the bare trailing `return &…;`
    if not default:
        raise SystemExit(f"agy_handler_for() in {path} has no default return")
    return dict(_HANDLER_CASE.findall(body)), default[-1]


def active_rows(rows):
    """Rows installed as static trampolines — the ones whose addresses must be exact."""
    return [r for r in rows if r["mech"] in ("fullcgo", "asmcgo")]


def hook_targets(rows):
    """Go symbols to resolve and export, in procdef order with the gateways last.

    Inactive (`AGY_OFF`) rows are kept: the C `agy_hook` constructor resolves every row in the
    table at compile time, and keeping their addresses means re-enabling one is a procdef edit
    rather than an archaeology session.
    """
    names, seen = [], set()
    for r in rows:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            names.append(r["symbol"])
    return names + [g for g in GATEWAY_TARGETS if g not in seen]


# --- ELF ----------------------------------------------------------------------------------------
class ELF:
    """Sections, segments and relocations of one ELF image, plus raw little-endian readers.

    lief does the structural parsing — it already ships for make_libagy.py's rewrite step, and
    using it here retired ~40 lines of hand-rolled e_shoff/e_phoff/shstrtab arithmetic. The raw
    bytes stay alongside it because walking Go's function table is millions of small integer
    reads, far cheaper through `struct` than through lief's object graph.
    """

    def __init__(self, data):
        _load_lief()
        self.d = data
        # lief takes bytes, not bytearray; callers that patch bytes keep their own mutable copy.
        self.bin = lief.parse(data if isinstance(data, bytes) else bytes(data))
        if self.bin is None:
            raise SystemExit("not an ELF (lief could not parse it)")
        self.u32 = lambda o: struct.unpack_from("<I", data, o)[0]
        self.i32 = lambda o: struct.unpack_from("<i", data, o)[0]
        self.u64 = lambda o: struct.unpack_from("<Q", data, o)[0]
        #: file-backed PT_LOAD windows as (vaddr, file offset, filesz)
        self.loads = [(s.virtual_address, s.file_offset, s.physical_size)
                      for s in self.bin.segments if s.type == lief.ELF.Segment.TYPE.LOAD]

    def sec(self, name):
        """The named lief `Section`, or None."""
        return self.bin.get_section(name)

    def v2o(self, vaddr):
        """Virtual address -> file offset, within a PT_LOAD's *file-backed* window.

        Deliberately not lief's `virtual_address_to_offset`, which also maps the .bss tail past
        p_filesz where no bytes exist. Everything here reads code or tables, and callers treat the
        ValueError as "this address is not present in the image".
        """
        for seg_vaddr, seg_off, filesz in self.loads:
            if seg_vaddr <= vaddr < seg_vaddr + filesz:
                return seg_off + (vaddr - seg_vaddr)
        raise ValueError(f"vaddr {vaddr:#x} not in any file-backed PT_LOAD")

    def relative_relocs(self):
        """Every `R_X86_64_RELATIVE` relocation, as (target vaddr, addend) pairs."""
        rel = lief.ELF.Relocation.TYPE.X86_64_RELATIVE
        return ((r.address, r.addend) for r in self.bin.relocations if r.type == rel)

    def build_id(self):
        for note in self.bin.notes:
            if note.type == lief.ELF.Note.TYPE.GNU_BUILD_ID:
                return bytes(note.description).hex()
        return ""


def find_pclntab(d: bytes):
    for m0 in (0xF1, 0xF0):
        i = 0
        magic = bytes([m0, 0xFF, 0xFF, 0xFF])
        while True:
            i = d.find(magic, i)
            if i < 0:
                break
            if d[i + 4] == 0 and d[i + 5] == 0 and d[i + 6] in (1, 2, 4) and d[i + 7] in (4, 8):
                return i  # fileoff; in seg0 fileoff==vaddr
            i += 4
    raise SystemExit("pclntab magic not found")


# --- Go function prologues ----------------------------------------------------------------------
# The stack-split check is `cmp 0x10(%r14),<reg>`: REX.WB (+opt R) 3B modrm 10, where modrm has
# mod=01, rm=110 (r14), i.e. (modrm & 0xC7) == 0x46. That covers the small-frame form (reg=rsp,
# REX 0x49) and the large-frame form after `lea -N(%rsp),%r12` (reg=r12, REX 0x4d).
def _stack_check_at(b, k):
    return (b[k] & 0xFB) == 0x49 and b[k + 1] == 0x3B and (b[k + 2] & 0xC7) == 0x46 and b[k + 3] == 0x10


def is_prologue(elf: ELF, d: bytes, addr: int) -> bool:
    """Whether `addr` looks like a Go function entry — the layout guard for a resolved hook."""
    try:
        b = d[elf.v2o(addr):elf.v2o(addr) + 24]
    except ValueError:
        return False
    for k in range(18):  # large-frame prologues put the cmp ~offset 12
        if _stack_check_at(b, k):
            return True
    # nosplit frames: push rbp (0x55) or sub $x,%rsp
    return b[:1] == b"\x55" or b[:3] in (b"\x48\x83\xec", b"\x48\x81\xec")


def prologue_skip(elf: ELF, d: bytes, addr: int) -> int:
    """Bytes to skip so the hook lands PAST the stack-check prologue — else Go's morestack re-runs
    the entry and re-enters our trampoline, stalling agy. Finds `cmp 0x10(%r14),reg` then the
    following Jcc (to the morestack stub); the real body begins right after. 0 for nosplit funcs."""
    try:
        b = d[elf.v2o(addr):elf.v2o(addr) + 48]
    except ValueError:
        return 0
    for k in range(40):
        if _stack_check_at(b, k):
            j = k + 4  # past the cmp
            if b[j] == 0x0F and b[j + 1] in (0x82, 0x83, 0x86, 0x87):  # jb/jae/jbe/ja rel32
                return j + 6
            if b[j] in (0x72, 0x73, 0x76, 0x77):                       # rel8
                return j + 2
            return j
    return 0


def frame_prologue_hex(elf: ELF, d: bytes, addr: int, skip: int) -> str:
    """The position-independent Go frame setup at `addr + skip` (`push %rbp; mov %rsp,%rbp;
    sub $imm8/imm32,%rsp` = 8 or 11 bytes), so the static trampoline stubs can bake the displaced
    instructions at compile time. Reconstructs the original losslessly when make_libagy.py has
    already patched bytes 0..5 with `ff 25 <disp32>` and stored `0x81`/`0x83` at byte 6."""
    if skip == 0:
        return ""
    try:
        p = d[elf.v2o(addr + skip):elf.v2o(addr + skip) + 16]
    except ValueError:
        return ""
    if len(p) >= 11 and p[:7] == b"\x55\x48\x89\xe5\x48\x81\xec":
        return p[:11].hex()
    if len(p) >= 8 and p[:7] == b"\x55\x48\x89\xe5\x48\x83\xec":
        return p[:8].hex()
    if len(p) >= 11 and p[:2] == b"\xff\x25" and p[6] == 0x81:
        return "554889e54881ec" + p[7:11].hex()
    if len(p) >= 8 and p[:2] == b"\xff\x25" and p[6] == 0x83:
        return "554889e54883ec" + p[7:8].hex()
    return ""


# --- Go image -----------------------------------------------------------------------------------
class GoImage:
    """A Go binary's pclntab: moduledata, the real text base, and the function table.

    agy has no ELF symtab and no standard Go buildinfo header, and its `pcHeader.textStart` is 0 —
    the real base is `moduledata.text`, a PIE-relocated pointer read out of `.rela.dyn`. gosym uses
    textStart and GoReSym cannot even find the pclntab, so both mis-resolve this binary. Addresses
    are file virtual addresses (PIE preferred base 0); at runtime add the load bias.
    """

    def __init__(self, data: bytes):
        self.d = data
        self.elf = ELF(data)
        u64 = self.elf.u64
        self.B = find_pclntab(data)              # pclntab vaddr (== fileoff in seg0)
        self.ptr_size = data[self.B + 7]
        self.nfunc = u64(self.B + 8)
        self._funcnametab = u64(self.B + 32)
        self._functab = self.B + u64(self.B + 64)

        # moduledata: the RELATIVE reloc whose addend is the pclntab vaddr.
        md = next((addr for addr, addend in self.elf.relative_relocs() if addend == self.B), None)
        if md is None:
            raise SystemExit("could not find moduledata (no reloc addend == pclntab)")
        self.md_vaddr = md

        # moduledata.text is itself relocated; read the addends in the struct's window.
        relwin = {addr: addend for addr, addend in self.elf.relative_relocs()
                  if md <= addr < md + 0x400}
        self.minpc = relwin.get(md + 0xA0)       # cross-check
        text_base = relwin.get(md + 0xB0)        # moduledata.text
        text_sec = self.elf.sec(".text")
        if text_sec is None:
            raise SystemExit("no .text section")
        text_lo, text_hi = text_sec.virtual_address, text_sec.virtual_address + 0x1000
        if text_base is None or not (text_lo <= text_base < text_hi):
            # Fallback: the lowest addend landing in the first page of .text.
            cands = sorted({v for v in relwin.values() if text_lo <= v < text_hi})
            text_base = cands[0] if cands else text_lo
        self.text_base = text_base

    def functions(self):
        """(addr, name) for every function in the table, in table order."""
        d, u32, i32 = self.d, self.elf.u32, self.elf.i32
        for i in range(self.nfunc):
            eo = u32(self._functab + i * 8)
            fo = u32(self._functab + i * 8 + 4)
            o = self.B + self._funcnametab + i32(self._functab + fo + 4)
            yield self.text_base + eo, d[o:d.index(b"\0", o)].decode("utf-8", "replace")

    def resolve(self, names):
        """{name: addr} for the names present, first definition winning (BOLT emits hot/cold
        copies of some functions; the lower entryoff is the one callers actually reach)."""
        wanted, found = set(names), {}
        for addr, nm in self.functions():
            if nm in wanted and nm not in found:
                found[nm] = addr
        return found

    def is_prologue(self, addr):
        return is_prologue(self.elf, self.d, addr)

    def prologue_skip(self, addr):
        return prologue_skip(self.elf, self.d, addr)

    def frame_prologue_hex(self, addr, skip):
        return frame_prologue_hex(self.elf, self.d, addr, skip)
