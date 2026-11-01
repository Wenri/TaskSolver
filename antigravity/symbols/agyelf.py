#!/usr/bin/env python3
"""Read the shared BOOKS table, ELF metadata and Go function tables for the agy build."""
import os
import re
import struct
from io import BytesIO
from functools import cached_property
from collections.abc import Iterator, Sequence
from typing import Literal, NamedTuple, Protocol, TypedDict, cast

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.dynamic import DynamicSegment
from elftools.elf.enums import ENUM_D_TAG, ENUM_RELOC_TYPE_x64
from elftools.elf.relocation import RelocationSection
from elftools.elf.sections import Section, Symbol, SymbolTableSection
from elftools.elf.segments import NoteSegment

from .x86 import (
    RegisterSpill, decode_frame, decode_stack_check, morestack_spills,
    planner_response_itab, stack_check_size,
)


# Shared by the host, generated assembly and Python book mirror.
PROCDEF_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "procdef.h"))

# Called by stubs, never booked. Synthetic ids give them the same export naming as book rows.
GATEWAYS = {"CGOCALL": "runtime.cgocall", "ASMCGOCALL": "runtime.asmcgocall"}
GATEWAY_TARGETS = tuple(GATEWAYS.values())

# Exported for the C++ host to start Go.
ENTRY_TARGET = "runtime.rt0_go"

type Mechanism = Literal["off", "fullcgo", "asmcgo"]


class BookTarget(TypedDict):
    id: str
    symbol: str


class Book(BookTarget):
    mode: Literal["sync", "async"]
    mech: Mechanism
    retcap: int


class PatchMetadata(TypedDict):
    books: dict[str, int]
    skips: dict[str, int]
    prologues: dict[str, str]


class EntryGCMap(TypedDict):
    reg_ptrmask: int
    stack_args_size: int
    stack_ptrmask: int
    args_size: int
    args_nbit: int
    args_ptrmask: int


class SymbolsMetadata(PatchMetadata):
    build_id: str
    agy_path: str
    ptr_size: int
    moduledata_vaddr: int
    text_base: int
    minpc: int | None
    total_funcs: int
    missing: list[str]
    gc_maps: dict[str, EntryGCMap]
    planner_response_itab: int


class MappedImage(Protocol):
    @property
    def loads(self) -> Sequence[tuple[int, int, int]]: ...


def c_sym_name(book_id: str) -> str:
    """The export name of a row's Go function: `agy_go__<id>`, mirroring `agy_stub__<id>`."""
    return "agy_go__" + book_id


def export_map(rows: Sequence[BookTarget]) -> dict[str, str]:
    """`{id: Go symbol}` for every row, disabled ones included, plus the cgocall gateways."""
    out = {r["id"]: r["symbol"] for r in rows}
    clash = set(out) & set(GATEWAYS)
    if clash:
        raise SystemExit(f"procdef book id(s) collide with the gateway ids: {sorted(clash)}")
    out.update(GATEWAYS)
    return out


# --- src/procdef.h ------------------------------------------------------------------------------
# A row: { "id", WIRE_x, retcap, AGY_FULLGO/AGY_ASMGO/AGY_OFF(<symbol>) }. The symbol is an optional
# MACRO followed by adjacent string literals, which C concatenates (e.g. CAC "StreamGenerateContent").
_LIT = r'"(?:[^"\\]|\\.)*"'
_ROW = re.compile(
    r'\{\s*"(?P<id>[a-z0-9_]+)"\s*,\s*'
    r'(?P<mode>WIRE_\w+)\s*,\s*'
    r'(?P<retcap>-?\d+)\s*,\s*'
    rf'(?P<mech>AGY_\w+)\(\s*(?P<sym>(?:[A-Z_][A-Z0-9_]*\s*)?(?:{_LIT}\s*)+)\)\s*\}}'
)
_MACRO_DEF = re.compile(rf'^\s*#define\s+([A-Z_][A-Z0-9_]*)\s+((?:{_LIT}\s*)+)$', re.M)


def _unquote(literals: str) -> str:
    """The value of adjacent C string literals."""
    return "".join(re.sub(r'\\(.)', r'\1', match[0][1:-1]) for match in re.finditer(_LIT, literals))


def parse_procdef(src_text: str) -> list[Book]:
    """Every row of BOOKS in procdef.h, in table order."""
    src_text = src_text.replace("\\\n", "")   # splice continued lines, as the preprocessor does
    macros = {match[1]: _unquote(match[2]) for match in _MACRO_DEF.finditer(src_text)}
    mechanisms: dict[str, Mechanism] = {"AGY_OFF": "off", "AGY_FULLGO": "fullcgo", "AGY_ASMGO": "asmcgo"}
    rows: list[Book] = []
    for m in _ROW.finditer(src_text):
        sym = m.group("sym")
        mac = re.match(r'\s*([A-Z_][A-Z0-9_]*)', sym)
        rows.append({
            "id": m.group("id"),
            "symbol": (macros.get(mac.group(1), "") if mac else "") + _unquote(sym),
            "mode": "sync" if m.group("mode") == "WIRE_SYNC" else "async",
            "mech": mechanisms[m.group("mech")],
            "retcap": int(m.group("retcap")),
        })
    return rows


def load_procdef(path: str | os.PathLike[str] | None = None) -> list[Book]:
    """Read BOOKS from procdef.h, failing if no rows are parsed."""
    path = path or PROCDEF_PATH
    with open(path) as f:
        rows = parse_procdef(f.read())
    if not rows:
        raise SystemExit(f"no book rows parsed from {path}")
    return rows


def active_rows(rows: Sequence[Book]) -> list[Book]:
    """Rows installed as static trampolines — the ones whose addresses must be exact."""
    return [r for r in rows if r["mech"] in ("fullcgo", "asmcgo")]


def book_targets(rows: Sequence[BookTarget]) -> list[str]:
    """Unique Go symbols in table order, including disabled rows, then gateways."""
    return list(dict.fromkeys([r["symbol"] for r in rows] + list(GATEWAY_TARGETS)))


# --- ELF ----------------------------------------------------------------------------------------
class Relocation(NamedTuple):
    address: int
    type: int
    symbol: str | None
    addend: int


class ELF:
    """Read ELF metadata with pyelftools and Go's private tables with integer readers."""

    def __init__(self, data: bytes) -> None:
        self.d = data
        try:
            self.elf = ELFFile(BytesIO(data))
        except ELFError as exc:
            raise SystemExit(f"not an ELF (pyelftools: {exc})") from exc
        if (self.elf.elfclass != 64 or not self.elf.little_endian
                or self.elf['e_machine'] != 'EM_X86_64'):
            raise SystemExit("agy tools require a little-endian ELF64 AMD64 image")
        # File-backed PT_LOAD ranges: (virtual address, file offset, file size).
        self.loads = [(s['p_vaddr'], s['p_offset'], s['p_filesz'])
                      for s in self.elf.iter_segments() if s['p_type'] == 'PT_LOAD']

    def u32(self, offset: int) -> int:
        return struct.unpack_from("<I", self.d, offset)[0]

    def i32(self, offset: int) -> int:
        return struct.unpack_from("<i", self.d, offset)[0]

    def u64(self, offset: int) -> int:
        return struct.unpack_from("<Q", self.d, offset)[0]

    def sec(self, name: str) -> Section | None:
        return self.elf.get_section_by_name(name)

    def v2o(self, vaddr: int, size: int = 1) -> int:
        """Map a complete span to a file offset, rejecting BSS and truncated segments."""
        if size < 1:
            raise ValueError("file-backed spans must contain at least one byte")
        for seg_vaddr, seg_off, filesz in self.loads:
            if seg_vaddr <= vaddr and vaddr + size <= seg_vaddr + filesz:
                offset = seg_off + (vaddr - seg_vaddr)
                if offset + size <= len(self.d):
                    return offset
        raise ValueError(f"vaddr {vaddr:#x} not in any file-backed PT_LOAD")

    @cached_property
    def relative_relocs(self) -> tuple[tuple[int, int], ...]:
        """Cached (address, addend) pairs; GoImage searches this large table twice."""
        relative = ENUM_RELOC_TYPE_x64['R_X86_64_RELATIVE']
        return tuple((r['r_offset'], r['r_addend'] if section.is_RELA() else 0)
                     for section in self.elf.iter_sections()
                     if isinstance(section, RelocationSection)
                     for r in section.iter_relocations() if r['r_info_type'] == relative)

    def plt_relocations(self) -> Iterator[Relocation]:
        """Yield the loader's DT_JMPREL entries, independently of relocation section headers."""
        for segment in self.elf.iter_segments():
            if not isinstance(segment, DynamicSegment):
                continue
            tags = {tag.entry.d_tag: tag.entry.d_val for tag in segment.iter_tags()}
            if 'DT_JMPREL' not in tags:
                continue
            size = tags.get('DT_PLTRELSZ', 0)
            if (tags.get('DT_PLTREL') != ENUM_D_TAG['DT_RELA'] or size <= 0 or size % 24):
                raise SystemExit("invalid AMD64 DT_JMPREL relocation table")
            self.v2o(tags['DT_JMPREL'], size)
            table = segment.get_relocation_tables()['JMPREL']
            names: dict[int, str] = {}
            for relocation in table.iter_relocations():
                index = relocation['r_info_sym']
                if index and index not in names:
                    names[index] = segment.get_symbol(index).name
                yield Relocation(relocation['r_offset'], relocation['r_info_type'],
                                 names.get(index), relocation['r_addend'])

    def dynamic_tags(self) -> dict[str | int, int]:
        """Map dynamic tag names to integer values; repeated tags retain their last value."""
        return {tag.entry.d_tag: tag.entry.d_val
                for segment in self.elf.iter_segments() if isinstance(segment, DynamicSegment)
                for tag in segment.iter_tags()}

    def symbol(self, name: str) -> Symbol | None:
        """Look up a symbol, preferring the dynamic table used by the loader."""
        for table in ('.dynsym', '.symtab'):
            section = self.sec(table)
            if isinstance(section, SymbolTableSection):
                matches = section.get_symbol_by_name(name)
                if matches:
                    return matches[0]
        return None

    def build_id(self) -> str:
        for segment in self.elf.iter_segments():
            if not isinstance(segment, NoteSegment):
                continue
            for note in segment.iter_notes():
                if note['n_name'] == 'GNU' and note['n_type'] == 'NT_GNU_BUILD_ID':
                    return cast(str, note['n_desc'])
        return ""


def find_pclntab(d: bytes) -> int:
    for m0 in (0xF1, 0xF0):
        i = 0
        magic = bytes([m0, 0xFF, 0xFF, 0xFF])
        while True:
            i = d.find(magic, i)
            if i < 0:
                break
            if (i + 8 <= len(d) and d[i + 4] == 0 and d[i + 5] == 0
                    and d[i + 6] in (1, 2, 4) and d[i + 7] in (4, 8)):
                return i  # fileoff; in seg0 fileoff==vaddr
            i += 4
    raise SystemExit("pclntab magic not found")


# --- Go function prologues ----------------------------------------------------------------------
def _entry_code(elf: MappedImage, data: bytes, address: int) -> bytes:
    """Read up to 64 instruction bytes within a file-backed segment."""
    for base, offset, filesz in elf.loads:
        if base <= address < base + filesz:
            offset += address - base
            size = min(64, base + filesz - address)
            return data[offset:offset + size]
    return b""


def prologue_skip(elf: MappedImage, d: bytes, addr: int) -> int:
    """Length of a valid stack check followed by a frame, or zero if unsupported.

    Books must follow the check so morestack cannot re-enter the trampoline.
    """
    return stack_check_size(_entry_code(elf, d, addr), addr)


def frame_prologue_hex(elf: MappedImage, d: bytes, addr: int, skip: int) -> str:
    """Original frame bytes at addr + skip, replayed by the generated stub."""
    if skip == 0:
        return ""
    code = _entry_code(elf, d, addr + skip)
    try:
        _, size = decode_frame(code)
    except ValueError:
        return ""
    return code[:size].hex()


def derive_entry_gc_map(args_size: int, nbit: int, pointer_mask: int,
                        spills: Sequence[RegisterSpill]) -> EntryGCMap:
    """Move pointer bits from compiler spill homes to the stub's saved GP words.

    ArgsPointerMaps[0] covers stack arguments followed by register spill homes.
    The morestack stores identify that boundary and each register's exact home.
    With no stores, retaining an all-dead argument area claims no extra roots.
    """
    if (args_size < 0 or args_size % 8 or not 0 <= nbit <= args_size // 8
            or pointer_mask < 0 or pointer_mask >> nbit):
        raise ValueError("invalid Go argument size or pointer bitmap")
    stack_size = min((spill.offset - 8 for spill in spills), default=args_size)
    if stack_size < 0 or stack_size % 8 or stack_size > 512:
        raise ValueError("stack argument bitmap exceeds its aligned 64-word capacity")
    stack_mask = pointer_mask & ((1 << (stack_size // 8)) - 1)
    register_mask = spill_pointer_mask = 0
    occupied: set[int] = set()
    registers: set[int] = set()
    for register, offset, size in spills:
        if (not 0 <= register < 9 or register in registers or size not in (1, 2, 4, 8)
                or offset < 8 or offset % size or offset + size > args_size + 8):
            raise ValueError("invalid or duplicate Go register spill")
        byte_range = set(range(offset, offset + size))
        if occupied & byte_range:
            raise ValueError("overlapping Go register spill homes")
        occupied.update(byte_range)
        registers.add(register)
        bit = 1 << (offset // 8 - 1)
        if pointer_mask & bit:
            if size != 8:
                raise ValueError("pointer bitmap marks a narrow scalar register spill")
            register_mask |= 1 << register
            spill_pointer_mask |= bit
    if pointer_mask != stack_mask | spill_pointer_mask:
        raise ValueError("pointer bitmap has an unaccounted register spill root")
    return {"reg_ptrmask": register_mask, "stack_args_size": stack_size,
            "stack_ptrmask": stack_mask, "args_size": args_size,
            "args_nbit": nbit, "args_ptrmask": pointer_mask}


# --- Go image -----------------------------------------------------------------------------------
class GoImage:
    """A Go binary's pclntab: moduledata, the real text base, and the function table.

    agy has no ELF symtab and no standard Go buildinfo header, and its `pcHeader.textStart` is 0 —
    the real base is `moduledata.text`, a PIE-relocated pointer read out of `.rela.dyn`. gosym uses
    textStart and GoReSym cannot even find the pclntab, so both mis-resolve this binary. Addresses
    are file virtual addresses (PIE preferred base 0); at runtime add the load bias.
    """

    def __init__(self, data: bytes) -> None:
        self.d = data
        self.elf = ELF(data)
        u64 = self.elf.u64
        self.B = find_pclntab(data)              # pclntab vaddr (== fileoff in seg0)
        self.ptr_size = data[self.B + 7]
        self.nfunc = u64(self.B + 8)
        self._funcnametab = u64(self.B + 32)
        self._functab = self.B + u64(self.B + 64)

        # moduledata: the RELATIVE reloc whose addend is the pclntab vaddr.
        md = next((addr for addr, addend in self.elf.relative_relocs if addend == self.B), None)
        if md is None:
            raise SystemExit("could not find moduledata (no reloc addend == pclntab)")
        self.md_vaddr = md

        # moduledata.text is itself relocated; read the addends in the struct's window.
        relwin = {addr: addend for addr, addend in self.elf.relative_relocs
                  if md <= addr < md + 0x400}
        self.minpc = relwin.get(md + 0xA0)       # cross-check
        text_base = relwin.get(md + 0xB0)        # moduledata.text
        text_sec = self.elf.sec(".text")
        if text_sec is None:
            raise SystemExit("no .text section")
        text_lo, text_hi = text_sec['sh_addr'], text_sec['sh_addr'] + 0x1000
        if text_base is None or not (text_lo <= text_base < text_hi):
            # Fallback: the lowest addend landing in the first page of .text.
            cands = sorted({v for v in relwin.values() if text_lo <= v < text_hi})
            text_base = cands[0] if cands else text_lo
        self.text_base = text_base

    @cached_property
    def _function_records(self) -> dict[int, tuple[int, int]]:
        """Entry address -> (_func file offset, end address), including the final sentinel."""
        u32 = self.elf.u32
        return {self.text_base + u32(self._functab + i * 8): (
                    self._functab + u32(self._functab + i * 8 + 4),
                    self.text_base + u32(self._functab + (i + 1) * 8))
                for i in range(self.nfunc)}

    @cached_property
    def _morestack_noctxt(self) -> int:
        name = "runtime.morestack_noctxt"
        found = self.resolve([name])
        if name not in found:
            raise ValueError(f"missing {name}")
        return found[name]

    def entry_gc_map(self, address: int) -> EntryGCMap:
        """Derive entry roots from the original Go stack map and validated slow path."""
        if self.ptr_size != 8:
            raise ValueError("entry GC metadata requires the amd64 Go pointer size")
        function, end = self._function_records[address]
        if (not 0 <= function <= len(self.d) - 44
                or self.elf.u32(function) != address - self.text_base):
            raise ValueError("invalid Go function metadata")
        args_size = self.elf.i32(function + 8)
        npcdata = self.elf.u32(function + 28)
        nfuncdata = self.d[function + 43]
        if nfuncdata < 1:
            raise ValueError("missing Go argument pointer map")
        data_offset = function + 44 + 4 * npcdata
        if data_offset + 4 * nfuncdata > len(self.d):
            raise ValueError("truncated Go function data table")
        map_offset = self.elf.u32(data_offset)  # FUNCDATA_ArgsPointerMaps == 0
        if map_offset == 0xffffffff:
            raise ValueError("missing Go argument pointer map")
        gofunc = next((value for target, value in self.elf.relative_relocs
                       if target == self.md_vaddr + 344), None)
        if gofunc is None:
            raise ValueError("missing moduledata.gofunc relocation")
        map_address = gofunc + map_offset
        offset = self.elf.v2o(map_address, 8)
        count, nbit = struct.unpack_from("<ii", self.d, offset)
        if count < 1 or args_size < 0 or not 0 <= nbit <= args_size // 8:
            raise ValueError("invalid Go argument pointer map dimensions")
        stride = (nbit + 7) // 8
        self.elf.v2o(map_address, 8 + count * stride)
        pointer_mask = int.from_bytes(self.d[offset + 8:offset + 8 + stride], "little")
        check = decode_stack_check(_entry_code(self.elf, self.d, address), address)
        if check is None or not address < check[1] < end:
            raise ValueError("morestack target is outside its Go function")
        start = self.elf.v2o(check[1], end - check[1])
        spills = morestack_spills(self.d[start:start + end - check[1]], check[1],
                                 address, self._morestack_noctxt)
        return derive_entry_gc_map(args_size, nbit, pointer_mask, spills)

    def planner_response_itab(self) -> int:
        name = "google3/third_party/gemini_coder/proto/gemini_coder_go_proto.(*Step).GetPlannerResponse"
        matches = {address for address, symbol in self.functions() if symbol == name}
        if len(matches) != 1:
            raise ValueError(f"missing or ambiguous {name}")
        address = matches.pop()
        value = planner_response_itab(_entry_code(self.elf, self.d, address), address)
        if value <= 0 or value % 8:
            raise ValueError("unaligned planner response itab")
        self.elf.v2o(value, 24)
        return value

    def functions(self) -> Iterator[tuple[int, str]]:
        """(addr, name) for every function in the table, in table order."""
        d, u32, i32 = self.d, self.elf.u32, self.elf.i32
        for i in range(self.nfunc):
            eo = u32(self._functab + i * 8)
            fo = u32(self._functab + i * 8 + 4)
            o = self.B + self._funcnametab + i32(self._functab + fo + 4)
            yield self.text_base + eo, d[o:d.index(b"\0", o)].decode("utf-8", "replace")

    def resolve(self, names: Sequence[str]) -> dict[str, int]:
        """{name: addr} for the names present, first definition winning (BOLT emits hot/cold
        copies of some functions; the lower entryoff is the one callers actually reach)."""
        wanted = set(names)
        found: dict[str, int] = {}
        for addr, nm in self.functions():
            if nm in wanted and nm not in found:
                found[nm] = addr
        return found

    def prologue_skip(self, addr: int) -> int:
        return prologue_skip(self.elf, self.d, addr)

    def frame_prologue_hex(self, addr: int, skip: int) -> str:
        return frame_prologue_hex(self.elf, self.d, addr, skip)
