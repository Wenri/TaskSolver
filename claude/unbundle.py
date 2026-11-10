"""Split Claude into its clean private Bun, editable source, assets and caches."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final, cast

from elftools.common.exceptions import ELFError
from elftools.construct import Container
from elftools.elf.constants import P_FLAGS, SH_FLAGS
from elftools.elf.elffile import ELFFile
from pwnlib.asm import asm
from pwnlib.util.misc import align

from .build_cli import CLIPin, _load_pin, matches


ROOT: Final = Path(__file__).resolve().parent
TRAILER: Final = b"\n---- Bun! ----\n"
PREFIX: Final = "/$bunfs/"
RECORD_SIZE: Final = 52
SOURCE_HASHES: Final = 1 << 5
SUPPORTED_FLAGS: Final = (1 << 14) - 1
# Minified JavaScript identifiers. Every source contract below is anchored on property names
# and string literals, captures the minified names it needs, and must match exactly once.
IDENTIFIER: Final = r"[A-Za-z_$][\w$]*"
NATIVE_CHECK_BYTES: Final = 48  # opening bytes of each called entry point libwire.node re-checks


@dataclass(frozen=True)
class BunModule:
    name: str
    record_offset: int
    contents_offset: int
    contents_length: int
    loader: int
    encoding: int

    @property
    def relative_path(self) -> str:
        return self.name.removeprefix(PREFIX)


@dataclass(frozen=True)
class BunGraph:
    modules: tuple[BunModule, ...]
    entry_index: int
    source_hashes_offset: int | None

    @property
    def entry(self) -> BunModule:
        return self.modules[self.entry_index]


@dataclass(frozen=True)
class BunNatives:
    """The private Bun's stripped entry points, as `bun_graph --natives` locates them."""

    module_bytecode_generator: int
    cached_bytecode_destructor: int
    mi_free: int
    bun_string_to_wtf: int
    latin1_clone: int
    bundled_source_clone_call: int


@dataclass(frozen=True)
class UnbundleResult:
    runtime: Path
    entrypoint: Path
    manifest: Path
    module_count: int
    bun: Path
    source: Path
    assets: Path
    modules: Path


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("Truncated Bun graph metadata")
    return int.from_bytes(data[offset:offset + 4], "little")


def _read_graph(binary: Path) -> BunGraph:
    """Validate the pinned Bun ELF layout without extracting module payloads.

    Layout follows oven-sh/bun src/standalone_graph/StandaloneModuleGraph.rs:
    an eight-byte section length, six pointer pairs per module, and a 32-byte
    Offsets record immediately before the trailer. Unknown layouts fail closed.
    """
    try:
        with binary.open("rb") as stream:
            elf = ELFFile(stream)
            if elf.elfclass != 64 or not elf.little_endian or elf["e_machine"] != "EM_X86_64":
                raise ValueError("Expected a little-endian x86-64 ELF")
            section = elf.get_section_by_name(".bun")
            if section is None:
                raise ValueError("Executable has no .bun section")
            section_offset = section["sh_offset"]
            data = section.data()
    except ELFError as error:
        raise ValueError("Invalid Claude ELF executable") from error
    if len(data) < 8 + 32 + len(TRAILER):
        raise ValueError("Truncated .bun section")
    if int.from_bytes(data[:8], "little") != len(data) - 8 or not data.endswith(TRAILER):
        raise ValueError("Unsupported Bun section length or trailer")
    payload = data[8:]
    footer = len(payload) - len(TRAILER) - 32
    if int.from_bytes(payload[footer:footer + 8], "little") != footer:
        raise ValueError("Unsupported Bun graph byte count")
    table = _u32(payload, footer + 8)
    table_length = _u32(payload, footer + 12)
    entry = _u32(payload, footer + 16)
    flags = _u32(payload, footer + 28)
    if flags & ~SUPPORTED_FLAGS:
        raise ValueError("Unsupported Bun graph flags")
    if not table_length or table_length % RECORD_SIZE or table + table_length > footer:
        raise ValueError("Invalid Bun module table")
    count = table_length // RECORD_SIZE
    if entry >= count:
        raise ValueError("Invalid Bun entry point")
    argv_start, argv_length = _u32(payload, footer + 20), _u32(payload, footer + 24)
    if argv_start + argv_length > footer:
        raise ValueError("Invalid Bun runtime arguments")
    hashes = table + table_length if flags & SOURCE_HASHES else None
    if hashes is not None and hashes + count * 4 > footer:
        raise ValueError("Truncated Bun source hashes")
    modules: list[BunModule] = []
    names: set[str] = set()
    spans: list[tuple[int, int, int, int]] = []
    if argv_length:
        spans.append((argv_start, argv_start + argv_length, -1, -1))
    for index in range(count):
        record = table + index * RECORD_SIZE
        for field in range(0, 48, 8):
            offset, length = _u32(payload, record + field), _u32(payload, record + field + 4)
            if offset + length > footer:
                raise ValueError("Bun module pointer is outside the graph")
            if length:
                if offset + length > table:
                    raise ValueError("Bun module payload overlaps graph metadata")
                spans.append((offset, offset + length, index, field))
        name_start, name_length = _u32(payload, record), _u32(payload, record + 4)
        try:
            name = payload[name_start:name_start + name_length].decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("Unsupported Bun module name") from error
        relative = name.removeprefix(PREFIX)
        if (not name.startswith(PREFIX + "root/")
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative)
                or any(part in ("", ".", "..") for part in relative.split("/"))):
            raise ValueError("Unsafe Bun module path")
        if name in names:
            raise ValueError("Duplicate Bun module path")
        names.add(name)
        encoding, loader, module_format, side = payload[record + 48:record + 52]
        if loader == 1:
            if (encoding, module_format, side) != (1, 1, 0):
                raise ValueError("Unsupported Bun JavaScript module format")
        elif (loader not in (5, 10, 13) or encoding not in (0, 1, 2)
              or module_format != 0 or side != 1):
            raise ValueError("Unsupported Bun asset format")
        start, length = _u32(payload, record + 8), _u32(payload, record + 12)
        if start + length >= footer or payload[start + length] != 0:
            raise ValueError("Bun module contents lack a bounded terminator")
        modules.append(BunModule(name, section_offset + 8 + record,
                                 section_offset + 8 + start, length, loader, encoding))
    for module in modules:
        if any(str(parent) in names for parent in PurePosixPath(module.name).parents):
            raise ValueError("Conflicting Bun file and directory paths")
    if modules[entry].loader != 1:
        raise ValueError("Bun entry point is not JavaScript")
    start = modules[entry].contents_offset - section_offset - 8
    end = start + modules[entry].contents_length + 1
    if end > table or any(start < stop and begin < end
                          for begin, stop, index, field in spans
                          if (index, field) != (entry, 8)):
        raise ValueError("Bun entry source overlaps another graph payload")
    return BunGraph(tuple(modules), entry,
                    None if hashes is None else section_offset + 8 + hashes)


BUN_PAYLOAD_SEGMENT = 0x6FB00002   # PT_BUN_PAYLOAD: spans the payload since Bun's ahead-of-time compiler
BUN_GRAPH_REQUIREMENT = "bun_graph from patchnar 489cfe38 or later (--clean, --extract --no-bytecode, --natives)"


def _bun_graph(executable: str, arguments: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run bun_graph; an older build that rejects one of the options is named as the cause."""
    try:
        return subprocess.run([executable, *arguments], check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as error:
        output = cast(object, error.stderr)
        stderr = output if isinstance(output, str) else ""
        if "unknown argument: --" in stderr:
            raise ValueError(f"{executable} is too old ({stderr.strip()}); the Claude build needs "
                             f"{BUN_GRAPH_REQUIREMENT}") from error
        raise
_SEGMENT_FIELDS = ("p_type", "p_offset", "p_vaddr", "p_paddr", "p_filesz", "p_memsz", "p_flags", "p_align")


def _clean_runtime(source: Path, target: Path, bun_graph: str) -> None:
    """Have bun_graph remove the application payload, then check every native allocation here.

    ``bun_graph --clean`` knows both of Bun's payload layouts: the writable PT_LOAD grown over
    the payload, and PT_BUN_PAYLOAD with up to three PT_LOAD pieces. Against the original, the
    program headers other than the payload's, every loaded section's bytes, the entry point and
    the cleared standalone-graph pointer are checked independently of it.
    """
    _ = _bun_graph(bun_graph, [str(source), "--clean", str(target)], timeout=300)
    target.chmod(0o755)
    with source.open("rb") as original, target.open("rb") as cleaned:
        old, new = ELFFile(original), ELFFile(cleaned)
        if (old.elfclass != 64 or old["e_phentsize"] != old.structs.Elf_Phdr.sizeof()
                or new["e_phoff"] != old["e_phoff"] or new["e_phnum"] != old["e_phnum"]):
            raise ValueError("Unsupported ELF program-header layout")
        application, data = old.get_section_by_name(".bun"), old.get_section_by_name(".data")
        if application is None or data is None or new.get_section_by_name(".bun") is not None:
            raise ValueError("Unexpected clean Bun ELF layout")
        if new["e_entry"] != old["e_entry"]:
            raise ValueError("bun_graph changed the private Bun entry point")
        start = application["sh_addr"]
        before, after = list(old.iter_segments()), list(new.iter_segments())
        spans = [index for index, header in enumerate(before) if header["p_type"] == BUN_PAYLOAD_SEGMENT]
        if len(spans) > 1:
            raise ValueError("Private Bun input maps its application twice")
        reserved: set[int] = set(spans)
        if spans:
            span = before[spans[0]]
            for index in range(spans[0] + 1, min(spans[0] + 4, len(before))):
                header = before[index]
                inside = (header["p_type"] == "PT_LOAD" and span["p_vaddr"] <= header["p_vaddr"]
                          and header["p_vaddr"] + header["p_memsz"] <= span["p_vaddr"] + span["p_memsz"])
                if header["p_type"] != "PT_NULL" and not inside:
                    break
                reserved.add(index)
        for index, (was, now) in enumerate(zip(before, after)):
            if index in reserved:
                if now["p_type"] != "PT_NULL" or any(now[key] for key in _SEGMENT_FIELDS[1:]):
                    raise ValueError("bun_graph kept a header of the application payload")
            elif (not spans and was["p_type"] == "PT_LOAD"
                  and was["p_vaddr"] <= start < was["p_vaddr"] + was["p_memsz"]):
                # The section layout: the writable PT_LOAD shrinks back to its native sections.
                if (any(now[key] != was[key] for key in _SEGMENT_FIELDS if key not in ("p_filesz", "p_memsz"))
                        or now["p_memsz"] < now["p_filesz"] or now["p_vaddr"] + now["p_memsz"] > start):
                    raise ValueError("bun_graph did not shrink the application's LOAD mapping")
            elif any(now[key] != was[key] for key in _SEGMENT_FIELDS):
                raise ValueError("bun_graph changed a native program header")
        for section in old.iter_sections():
            if not section["sh_flags"] & SH_FLAGS.SHF_ALLOC or section.name == ".bun":
                continue
            copy = new.get_section_by_name(section.name)
            # A NOBITS section (.bss, .tbss) has no file bytes: its offset is nominal, and the
            # program headers place it in memory.
            stored = section["sh_type"] != "SHT_NOBITS"
            if (copy is None or any(copy[key] != section[key] for key in ("sh_type", "sh_addr", "sh_size"))
                    or stored and (copy["sh_offset"] != section["sh_offset"] or copy.data() != section.data())):
                raise ValueError(f"bun_graph changed native section {section.name}")
        # The linked `.bun`, BUN_COMPILED in the padding after .data, holds the payload's
        # address: the clean runtime must read zero there and map nothing at that address.
        marker = -(-(data["sh_offset"] + data["sh_size"]) // 8) * 8
        original.seek(marker)
        cleaned.seek(marker)
        if int.from_bytes(original.read(8), "little") != start or cleaned.read(8) != bytes(8):
            raise ValueError("Private Bun standalone-graph pointer was not cleared")
        for header in after:
            if (header["p_type"] in ("PT_LOAD", BUN_PAYLOAD_SEGMENT)
                    and header["p_vaddr"] <= start < header["p_vaddr"] + header["p_memsz"]):
                raise ValueError("Clean Bun still maps the removed application payload")


def _append_utf8_shim(runtime: Path, code: bytes, natives: BunNatives) -> list[dict[str, str | int]]:
    """Append compiled code without moving native sections or rewriting dynamic metadata.

    The already-bundled source loader clones file contents as Latin-1; its one call is
    redirected to the shim, which builds the same string from UTF-8 instead.
    """
    if not code or len(code) > 4096:
        raise ValueError("Unexpected UTF-8 loader shim size")
    call_address = natives.bundled_source_clone_call
    original_call = b"\xe8" + (natives.latin1_clone - call_address - 5).to_bytes(4, "little", signed=True)
    with runtime.open("r+b") as stream:
        elf = ELFFile(stream)
        phdr, shdr = elf.structs.Elf_Phdr, elf.structs.Elf_Shdr
        if (elf.elfclass != 64 or not elf.little_endian or elf["e_machine"] != "EM_X86_64"
                or elf["e_type"] != "ET_EXEC" or elf["e_phentsize"] != phdr.sizeof()
                or elf["e_shentsize"] != shdr.sizeof()
                or elf.get_section_by_name(".tasksolver.utf8") is not None):
            raise ValueError("Unsupported private Bun ELF layout for UTF-8 shim")
        string_index = elf["e_shstrndx"]
        strings = elf.get_section(string_index).data()
        offsets = list(elf.address_offsets(call_address, 5))
        if len(offsets) != 1:
            raise ValueError("Private Bun source-loader call is not mapped uniquely")
        call_offset = offsets[0]
        stream.seek(call_offset)
        if stream.read(5) != original_call:
            raise ValueError("Private Bun source-loader call signature changed")
        headers = [segment.header.copy() for segment in elf.iter_segments()]
        sections = [section.header.copy() for section in elf.iter_sections()]
        end_address = max(segment["p_vaddr"] + segment["p_memsz"]
                          for segment in elf.iter_segments() if segment["p_type"] == "PT_LOAD")
        # Leave the former .bun start unmapped. BUN_COMPILED.size is already zero.
        address = align(4096, end_address) + 4096
        offset = align(4096, stream.seek(0, os.SEEK_END))
        table_size = (len(headers) + 1) * phdr.sizeof()
        code_start = align(16, table_size)
        target = address + code_start
        replacement = asm(f"call {target:#x}", vma=call_address,
                          arch="amd64", bits=64, endian="little", os="linux")
        if len(replacement) != len(original_call) or replacement[:1] != b"\xe8":
            raise ValueError("UTF-8 shim call must be a five-byte near call")
        phdr_found = False
        for header in headers:
            if header["p_type"] == "PT_PHDR":
                header.update(p_offset=offset, p_vaddr=address, p_paddr=address,
                              p_filesz=table_size, p_memsz=table_size)
                phdr_found = True
        if not phdr_found:
            raise ValueError("Private Bun is missing PT_PHDR")
        length = code_start + len(code)
        headers.append(Container(p_type="PT_LOAD", p_flags=P_FLAGS.PF_R | P_FLAGS.PF_X,
                                 p_offset=offset, p_vaddr=address, p_paddr=address,
                                 p_filesz=length, p_memsz=length, p_align=4096))
        string_offset = offset + length
        name_offset = len(strings)
        strings += b".tasksolver.utf8\0"
        section_offset = align(8, string_offset + len(strings))
        sections[string_index].update(sh_offset=string_offset, sh_size=len(strings))
        sections.append(Container(sh_name=name_offset, sh_type="SHT_PROGBITS",
                                  sh_flags=SH_FLAGS.SHF_ALLOC | SH_FLAGS.SHF_EXECINSTR,
                                  sh_addr=target, sh_offset=offset + code_start, sh_size=len(code),
                                  sh_link=0, sh_info=0, sh_addralign=16, sh_entsize=0))
        stream.seek(offset)
        for header in headers:
            phdr.build_stream(header, stream)
        stream.seek(offset + code_start)
        _ = stream.write(code)
        stream.seek(string_offset)
        _ = stream.write(strings)
        stream.seek(section_offset)
        for section in sections:
            shdr.build_stream(section, stream)
        stream.seek(0)
        elf.header.update(e_phoff=offset, e_shoff=section_offset,
                          e_phnum=len(headers), e_shnum=len(sections))
        elf.structs.Elf_Ehdr.build_stream(elf.header, stream)
        stream.seek(call_offset)
        _ = stream.write(replacement)
    return [{"name": "external_source_utf8", "address": call_address,
             "original": original_call.hex(), "replacement": replacement.hex(),
             "target": target, "section": ".tasksolver.utf8"}]


def _mapped_code(elf: ELFFile, address: int, length: int) -> bool:
    return any(segment["p_type"] == "PT_LOAD" and segment["p_flags"] & P_FLAGS.PF_X
               and segment["p_vaddr"] <= address
               and address + length <= segment["p_vaddr"] + segment["p_filesz"]
               for segment in elf.iter_segments())


def _read_code(elf: ELFFile, stream: BinaryIO, address: int, length: int) -> bytes:
    offsets = list(elf.address_offsets(address, length))
    if len(offsets) != 1:
        raise ValueError(f"Private Bun code at {address:#x} is not mapped uniquely")
    _ = stream.seek(offsets[0])
    return stream.read(length)


def _read_natives(source: Path, bun_graph: str) -> BunNatives:
    """Locate the private Bun's stripped entry points with bun_graph, then check them here.

    bun_graph finds each one by its instructions, so the addresses follow the pinned build;
    the report is trusted only for addresses inside its code that this module verifies.
    """
    try:
        completed = subprocess.run([bun_graph, str(source), "--natives"], check=True,
                                   capture_output=True, text=True, timeout=300)
    except subprocess.CalledProcessError as error:
        raise ValueError("bun_graph --natives could not locate the private Bun entry points "
                         f"(it needs patchnar's native discovery): {cast(object, error.stderr)}") from error
    document = cast(object, json.loads(completed.stdout))
    if not isinstance(document, dict) or cast(dict[str, object], document).get("format") != 1:
        raise ValueError("bun_graph --natives returned an unsupported report")
    reported = cast(dict[str, object], document).get("natives")
    if not isinstance(reported, dict):
        raise ValueError("bun_graph --natives reported no entry points")
    values = cast(dict[str, object], reported)
    addresses: dict[str, int] = {}
    for field in fields(BunNatives):
        value = values.get(field.name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"bun_graph --natives did not locate {field.name}")
        addresses[field.name] = value
    natives = BunNatives(**addresses)
    with source.open("rb") as stream:
        elf = ELFFile(stream)
        for name, address in addresses.items():
            length = 5 if name == "bundled_source_clone_call" else NATIVE_CHECK_BYTES
            if not _mapped_code(elf, address, length):
                raise ValueError(f"bun_graph located {name} outside the private Bun's code")
        call = _read_code(elf, stream, natives.bundled_source_clone_call, 5)
    if (call[0] != 0xE8 or natives.bundled_source_clone_call + 5
            + int.from_bytes(call[1:], "little", signed=True) != natives.latin1_clone):
        raise ValueError("bun_graph's source clone call does not call its Latin-1 clone")
    return natives


def _natives_header(runtime: Path, natives: BunNatives) -> str:
    """native_cache.c's private Bun ABI: the located addresses and the code libwire.node re-checks."""
    with runtime.open("rb") as stream:
        elf = ELFFile(stream)

        def code(address: int) -> str:
            return "".join(f"\\x{byte:02x}" for byte in _read_code(elf, stream, address, NATIVE_CHECK_BYTES))

        return "\n".join((
            "/* Generated by claude/unbundle.py from `bun_graph --natives`. */",
            f"#define WIRE_BUN_TO_WTF {natives.bun_string_to_wtf:#x}",
            f"#define WIRE_BUN_EMIT {natives.module_bytecode_generator:#x}",
            f"#define WIRE_BUN_DESTROY {natives.cached_bytecode_destructor:#x}",
            f"#define WIRE_BUN_FREE {natives.mi_free:#x}",
            f'#define WIRE_BUN_EMIT_CODE "{code(natives.module_bytecode_generator)}"',
            f'#define WIRE_BUN_DESTROY_CODE "{code(natives.cached_bytecode_destructor)}"',
            f'#define WIRE_BUN_FREE_CODE "{code(natives.mi_free)}"')) + "\n"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _prepare_code(directory: Path, output: Path, graph: BunGraph) -> dict[str, dict[str, str | int]]:
    """Separate code, UTF-8 text, binary assets and native addons."""
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("bun_graph replaced its output directory")
    names = {str(directory / module.relative_path): module.name for module in graph.modules}
    # Match whole path literals so an asset named x cannot rewrite JS named x.js.
    pattern = re.compile(re.escape(str(directory) + "/") + r'''[^"'`\\\x00-\x1f]*''')
    modules: dict[str, dict[str, str | int]] = {}
    expected = {module.relative_path for module in graph.modules}
    for path in directory.rglob("*"):
        if path.is_symlink() or path.is_file() and path.relative_to(directory).as_posix() not in expected:
            raise ValueError(f"Unexpected bun_graph output: {path}")
    for module in graph.modules:
        path = directory / module.relative_path
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"bun_graph did not extract {module.name}")
        category = "source" if module.loader == 1 else "modules" if module.loader == 10 else "assets"
        destination = output / category / module.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if module.loader == 1:
            text = path.read_text(encoding="utf-8")
            text = pattern.sub(lambda match: names.get(match.group(), match.group()), text)
            if str(directory) in text:
                raise ValueError(f"Unrecognized extracted path in {module.name}")
            _ = destination.write_text(text, encoding="utf-8")
        elif module.loader == 13:
            encoding = "utf-16-le" if module.encoding == 2 else "latin-1"
            _ = destination.write_bytes(path.read_bytes().decode(encoding).encode("utf-8"))
        else:
            shutil.copyfile(path, destination)
        modules[module.name] = {"loader": module.loader,
                                "path": destination.relative_to(output).as_posix(),
                                "encoding": module.encoding}
    return modules


_IMPORT: Final = re.compile(r'import\{([^}]*)\}from"([^"]+)"')
_EXPORT: Final = re.compile(r"export\{([^}]*)\}")
_REQUIRE: Final = re.compile(r"(?<![\w$.])(" + IDENTIFIER + r")=import\.meta\.require(?![\w$(])")
_CLASSIFIER: Final = re.compile(
    r"function (" + IDENTIFIER + r')\(\)\{return typeof Bun<"u"&&Bun\.isStandaloneExecutable===!0\}')
_DEEP_LINK: Final = re.compile(r"if\(" + IDENTIFIER + r'\(\)\.disableDeepLinkRegistration==="disable"\)return;')


def _code_texts(directory: Path, graph: BunGraph) -> dict[str, str]:
    """Every extracted JavaScript module's text, keyed by its /$bunfs/ name."""
    return {module.name: (directory / module.relative_path).read_text(encoding="utf-8")
            for module in graph.modules if module.loader == 1}


def _sole(pattern: re.Pattern[str], texts: Mapping[str, str], contract: str,
          literal: str) -> tuple[str, re.Match[str]]:
    """The one match of a source contract across modules (prefiltered by a literal it contains)."""
    found = [(name, match) for name, text in texts.items() if literal in text
             for match in pattern.finditer(text)]
    if len(found) != 1:
        raise ValueError(f"Claude source contract '{contract}' matched {len(found)} times, not once")
    return found[0]


def _bindings(clause: str) -> list[tuple[str, str]]:
    """`a,b as c` from an import or export clause, as (name, alias) pairs."""
    pairs: list[tuple[str, str]] = []
    for item in clause.split(","):
        name, _, alias = item.strip().partition(" as ")
        if name:
            pairs.append((name, alias or name))
    return pairs


def _exported(text: str, local: str, contract: str) -> str:
    names = {alias for clause in _EXPORT.finditer(text)
             for name, alias in _bindings(clause.group(1)) if name == local}
    if len(names) != 1:
        raise ValueError(f"Claude source contract '{contract}' is not exported once")
    return names.pop()


def _imported(text: str, module: str, name: str, contract: str) -> str:
    """The binding `text` gives the export `name` of `module`."""
    names = {alias for clause in _IMPORT.finditer(text) if clause.group(2) == module
             for original, alias in _bindings(clause.group(1)) if original == name}
    if len(names) != 1:
        raise ValueError(f"Claude source contract '{contract}' is not imported once")
    return names.pop()


def _require_aliases(texts: Mapping[str, str]) -> set[str]:
    """Names for import.meta.require: the bundler runtime's binding and its importers' names."""
    aliases: set[str] = set()
    for module, text in texts.items():
        if "=import.meta.require" not in text:
            continue
        for match in _REQUIRE.finditer(text):
            local = match.group(1)
            aliases.add(local)
            exported = {alias for clause in _EXPORT.finditer(text)
                        for name, alias in _bindings(clause.group(1)) if name == local}
            for other in texts.values():
                for clause in _IMPORT.finditer(other):
                    if clause.group(2) == module:
                        aliases.update(alias for name, alias in _bindings(clause.group(1)) if name in exported)
    return aliases


def _install_loader(directory: Path, graph: BunGraph) -> dict[str, str]:
    """Resolve extracted modules and filesystem assets relative to the installation."""
    texts = _code_texts(directory, graph)
    code_names = set(texts)
    by_name = {module.name: module for module in graph.modules}
    worker = PREFIX + "root/src/plugins/functionHooks/hooks-worker/hooks-worker.js"
    pattern = re.compile(r'''(["'])(/\$bunfs/[^"'\n]+)\1''')
    # Module paths passed to import syntax or to the bundler's import.meta.require stay
    # /$bunfs/ names for the resolver; every other literal must name a relocatable file.
    requires = "".join(rf"|(?<![\w$.]){re.escape(alias)}\(" for alias in sorted(_require_aliases(texts)))
    importing = re.compile(r"(?:from|import|import\(|\.require\(" + requires + r")$")
    for name, text in texts.items():
        module = by_name[name]

        def relocate(match: re.Match[str]) -> str:
            path = match.group(2)
            target = by_name.get(path)
            if target is None:
                return match.group()
            if importing.search(text, max(0, match.start() - 30), match.start()):
                return match.group()
            if target.loader != 5 and path != worker:
                raise ValueError(f"Unsupported non-import JavaScript path in {module.name}: {path}")
            physical = ("../assets/" if target.loader == 5 else "") + target.relative_path
            relative = posixpath.relpath(physical,
                                        posixpath.dirname(module.relative_path))
            return f"Bun.fileURLToPath(new URL({json.dumps('./' + relative)},import.meta.url))"

        texts[name] = pattern.sub(relocate, text)
    # The native package classifier keys Claude's packaged-runtime paths off the compiled
    # executable; the split runtime is that same package.
    classifier, match = _sole(_CLASSIFIER, texts, "native-package classifier", "Bun.isStandaloneExecutable")
    text = texts[classifier]
    texts[classifier] = text[:match.start()] + f"function {match.group(1)}(){{return!0}}" + text[match.end():]
    # Interactive sessions register the OS claude-cli:// handler as ~/.local/bin/claude or
    # process.execPath, this installation's launcher, and xdg-mime then rewrites mimeapps.list;
    # a packaged runtime skips that, as disableDeepLinkRegistration does.
    deep_link, match = _sole(_DEEP_LINK, texts, "deep-link registration", "disableDeepLinkRegistration")
    text = texts[deep_link]
    texts[deep_link] = text[:match.start()] + "if(!0)return;" + text[match.end():]
    for name, text in texts.items():
        _ = (directory / by_name[name].relative_path).write_text(text, encoding="utf-8")
    paths = {name: "./" + name.removeprefix(PREFIX) for name in code_names}
    for index, module in enumerate(graph.modules):
        if module.loader == 10:
            paths[module.name] = "../modules/" + module.relative_path
        elif module.loader == 13:
            adapter = directory / "tasksolver-assets" / f"{index}.cjs"
            adapter.parent.mkdir(exist_ok=True)
            relative = "../../assets/" + module.relative_path
            _ = adapter.write_text("module.exports=require('node:fs').readFileSync("
                                    "require('node:path').join(__dirname," + json.dumps(relative) +
                                    "),'utf8');\n", encoding="utf-8")
            paths[module.name] = "./" + adapter.relative_to(directory).as_posix()
    resolver = directory / "tasksolver-resolver.mjs"
    code = ("const modules=" + json.dumps(paths, sort_keys=True) + ";\n"
            "Bun.plugin({name:'tasksolver-claude-graph',setup(build){"
            "build.onResolve({filter:/^\\/\\$bunfs\\//},({path})=>"
            "Object.hasOwn(modules,path)?{path:Bun.fileURLToPath(new URL(modules[path],import.meta.url))}:undefined);}});\n")
    _ = resolver.write_text(code, encoding="utf-8")
    loader = directory / "tasksolver-entry.mjs"
    _ = loader.write_text("import './tasksolver-resolver.mjs';\n"
                          "process.execPath=Bun.fileURLToPath(new URL('../claude',import.meta.url));\n"
                          "process.argv[0]='bun';process.argv[1]='/$bunfs/root/cli';\n"
                          "process.env.DISABLE_AUTOUPDATER='1';\nawait import(" +
                          json.dumps("./" + graph.entry.relative_path) + ");\n", encoding="utf-8")
    if worker in code_names:
        path = directory / worker.removeprefix(PREFIX)
        original = path.with_name("hooks-worker.tasksolver-source.js")
        path.rename(original)
        relative = posixpath.relpath("tasksolver-resolver.mjs",
                                    posixpath.dirname(worker.removeprefix(PREFIX)))
        _ = path.write_text(f"import {json.dumps(relative)};\n"
                            f"await import({json.dumps('./' + original.name)});\n", encoding="utf-8")
    return {"native_package_classifier": classifier, "deep_link_registration": deep_link}


_CONSUMER: Final = re.compile(r"async function " + IDENTIFIER + r"\((\{engine:[^{}]*\})\)\{")
_COMPONENT: Final = re.compile(r"function " + IDENTIFIER + r"\((\{turn:[^{}]*\})\)\{")
_FUNCTION: Final = re.compile(r"(?:async )?function " + IDENTIFIER + r"\(")
_AGENT: Final = re.compile(r"agentId:(" + IDENTIFIER + r')\(\),mode:"prompt",priority:"next",'
                           r'origin:\{kind:"human"\},skipSlashCommands:!0')
_EFFECT: Final = re.compile(r"(?<![\w$])(" + IDENTIFIER + r")=function\((" + IDENTIFIER + r"),(" + IDENTIFIER
                            + r")\)\{return " + IDENTIFIER + r"\.H\.useEffect\(\2,\3\)\}")
_QUEUE_HOOK: Final = re.compile(r"function (" + IDENTIFIER + r")\(\)\{return " + IDENTIFIER + r"\(("
                                + IDENTIFIER + r")\)\?\?" + IDENTIFIER + r"\(\)\}")
_QUEUE_PROVIDER: Final = re.compile(r"\{queue:(" + IDENTIFIER + r"),children:" + IDENTIFIER + r"\}=")
_RESPONSE_PARSE: Final = re.compile(
    r"async function " + IDENTIFIER + r"\((" + IDENTIFIER + r"),(" + IDENTIFIER + r")\)\{"
    r"let\{response:(" + IDENTIFIER + r"),requestLogID:(" + IDENTIFIER + r"),"
    r"retryOfRequestLogID:(" + IDENTIFIER + r"),startTime:(" + IDENTIFIER + r")\}=\2,"
    r"(" + IDENTIFIER + r")=await\(async\(\)=>\{[\s\S]{100,2500}?`"
    r"\[\$\{\4\}\] response parsed`,\{retryOfRequestLogID:\5,url:\3\.url,status:\3\.status,"
    r"body:\7,durationMs:Date\.now\(\)-\6\}\),\7\}"
)
# Members of Claude's turn, host and command queue that the mounted bridge calls.
_BRIDGE_MEMBERS: Final = ("._engine", "._requireHost()", ".addNotification(", ".isExternalLoading",
                          ".guard.isActive", ".getCommandQueue()", "turnAttributionKey:", '.cancel("remote")')
_MOUNT: Final = (
    "let tsQueue={queue}();{effect}(()=>TS_LOCAL.attach({engine:{turn}._engine,get sessionId(){return {session}.id},"
    "enqueue:(frame)=>tsQueue.enqueue({value:frame.message.content,mode:\"prompt\","
    "agentId:{agent}(),uuid:frame.uuid,turnAttributionKey:frame.uuid,"
    "origin:{kind:\"human\"},skipSlashCommands:!0}),"
    "isBusy:()=>{turn}.guard.isActive||{turn}.isExternalLoading,"
    "hasQueued:(uuid)=>tsQueue.getCommandQueue().some((command)=>command.uuid===uuid),"
    "onReady:(socketPath)=>{turn}._requireHost().addNotification({key:\"tasksolver-local-bridge-ready\","
    "kind:\"feedback\",text:\"Python SDK socket: \"+socketPath,priority:\"immediate\",timeoutMs:15000}),"
    "onError:(message)=>{turn}._requireHost().addNotification({key:\"tasksolver-local-bridge-error\","
    "kind:\"warning\",text:message,priority:\"immediate\"}),"
    "interrupt:()=>{turn}.cancel(\"remote\")}),[{turn},{session}.id,tsQueue]);"
)


def _parameters(clause: str, contract: str, keys: tuple[str, ...]) -> dict[str, str]:
    """A function's `{key:local,...}` destructured parameters (no defaults or nesting)."""
    names: dict[str, str] = {}
    for item in clause[1:-1].split(","):
        key, _, local = item.partition(":")
        if not re.fullmatch(IDENTIFIER, key) or not re.fullmatch(IDENTIFIER, local):
            raise ValueError(f"Claude source contract '{contract}' has unsupported parameters")
        names[key] = local
    if missing := [key for key in keys if key not in names]:
        raise ValueError(f"Claude source contract '{contract}' lacks parameters {missing}")
    return names


def _within(pattern: str, text: str, header: re.Match[str], contract: str) -> re.Match[str]:
    """The one match of `pattern` in the body that follows `header`, up to the next function."""
    following = _FUNCTION.search(text, header.end())
    end = len(text) if following is None else following.start()
    found = list(re.compile(pattern).finditer(text, header.end(), end))
    if len(found) != 1:
        raise ValueError(f"Claude source contract '{contract}' matched {len(found)} times, not once")
    return found[0]


def _patch_native_bridge(directory: Path, graph: BunGraph) -> dict[str, str]:
    """Attach to the native prompt queue and tee its sole SDK-event consumer.

    Each edit is found by Claude's property names and string literals, never by its
    minified names or chunk files, so a release that keeps these code shapes needs no port.
    """
    texts = _code_texts(directory, graph)
    edits: dict[str, list[tuple[int, int, str]]] = {}
    # The turn consumer: tag the queued prompt's user message and tee every engine frame.
    consumer_module, consumer = _sole(_CONSUMER, texts, "SDK turn consumer", "({engine:")
    text = texts[consumer_module]
    names = _parameters(consumer.group(1), "SDK turn consumer", ("engine", "feed", "newMessages", "onRead"))
    engine, feed = names["engine"], names["feed"]
    user = _within(r"let (" + IDENTIFIER + r")=" + re.escape(names["newMessages"]) + r"\.findLast\(\(("
                   + IDENTIFIER + r')\)=>\2\.type==="user"\);', text, consumer, "turn user message").group(1)
    enqueue = _within(re.escape(f'{feed}.input.enqueue({{type:"user",message:{user}?.message??'
                                '{role:"user",content:""},parent_tool_use_id:null});'),
                      text, consumer, "turn input enqueue")
    release = _within(r"(" + IDENTIFIER + r")=await " + re.escape(feed) + r"\.readLock\.acquire\(\)",
                      text, consumer, "turn read lock").group(1)
    loop = _within(r"let (" + IDENTIFIER + r");try\{" + re.escape(names["onRead"]) + r"\?\.\(\);while\(!0\)\{let\{value:("
                   + IDENTIFIER + r"),done:(" + IDENTIFIER + r")\}=await " + re.escape(engine) + r"\.next\(\);if\(\3\)break;",
                   text, consumer, "turn event loop")
    result, frame, done = loop.group(1), loop.group(2), loop.group(3)
    final = _within(re.escape(f"}}finally{{{release}()}}return {result}}}"), text, consumer, "turn release")
    edits[consumer_module] = [
        (enqueue.end() - 3, enqueue.end() - 3, f",uuid:TS_LOCAL.userUuid({engine},{user}?.uuid)"),
        (loop.start(), loop.end(),
         f"let {result};try{{TS_LOCAL.begin({engine},{user}?.uuid);{names['onRead']}?.();while(!0){{"
         f"let{{value:{frame},done:{done}}}=await {engine}.next();if({done})break;"
         f"TS_LOCAL.emit({engine},{user}?.uuid,{frame});"),
        (final.start() + len("}finally{"), final.start() + len("}finally{"),
         f"TS_LOCAL.finish({engine},{user}?.uuid);"),
    ]
    # The native turn's component, the one {turn:...} component that renders the session's
    # tools: mount the bridge on its session, turn and prompt queue before that return.
    session_view = (r"return " + IDENTIFIER + r"\((?:" + IDENTIFIER + r",)*\(\)=>\(\{session:(" + IDENTIFIER
                    + r"),tools:")
    components: list[tuple[str, re.Match[str], re.Match[str]]] = []
    for name, other in texts.items():
        for header in (_COMPONENT.finditer(other) if "({turn:" in other else ()):
            try:
                components.append((name, header, _within(session_view, other, header, "native turn session")))
            except ValueError:
                continue
    if len(components) != 1:
        raise ValueError(f"Claude source contract 'native turn component' matched {len(components)} times, not once")
    component_module, component, view = components[0]
    text = texts[component_module]
    turn = _parameters(component.group(1), "native turn component", ("turn",))["turn"]
    agents = {match.group(1) for match in _AGENT.finditer(text)}
    if len(agents) != 1:
        raise ValueError("Claude source contract 'prompt agent id' did not name one function")
    react, effect = _sole(_EFFECT, texts, "React useEffect", ".H.useEffect(")
    queues = [(name, hook.group(1)) for name, other in texts.items()
              if "{queue:" in other and ".Provider,{value:" in other
              for hook in _QUEUE_HOOK.finditer(other)
              for provider in _QUEUE_PROVIDER.finditer(other)
              if f"{hook.group(2)}.Provider,{{value:{provider.group(1)}," in other]
    if len(queues) != 1:
        raise ValueError(f"Claude source contract 'command queue hook' matched {len(queues)} times, not once")
    queue_module, queue = queues[0]
    bound = {
        "turn": turn, "session": view.group(1), "agent": agents.pop(),
        "effect": _imported(text, react, _exported(texts[react], effect.group(1), "React useEffect"),
                            "React useEffect"),
        "queue": _imported(text, queue_module, _exported(texts[queue_module], queue, "command queue hook"),
                           "command queue hook"),
    }
    if missing := [member for member in _BRIDGE_MEMBERS if not any(member in other for other in texts.values())]:
        raise ValueError(f"Claude source contract 'bridge members' lacks {missing}")
    mount = re.sub(r"\{(turn|session|agent|effect|queue)\}", lambda match: bound[match.group(1)], _MOUNT)
    edits.setdefault(component_module, []).append((view.start(), view.start(), mount))
    parse_module, parse_match = _sole(_RESPONSE_PARSE, texts, "SDK response parser", "] response parsed")
    info_var, body_var = parse_match.group(2), parse_match.group(7)
    suffix = f",{body_var}}}"
    if not parse_match.group(0).endswith(suffix):
        raise ValueError("Claude source contract 'SDK response parser' has unexpected return tail")
    edits.setdefault(parse_module, []).append(
        (parse_match.end() - len(suffix), parse_match.end(), f",TS_LOCAL.tapResponse({info_var},{body_var})}}")
    )
    helper = directory / "tasksolver-native-bridge.mjs"
    shutil.copyfile(ROOT / "native_bridge.mjs", helper)
    by_name = {item.name: item for item in graph.modules}
    for name, changes in edits.items():
        text = texts[name]
        position = len(text)
        for start, end, replacement in sorted(changes, reverse=True):
            if end > position:
                raise ValueError("Claude source contracts overlap")
            text, position = text[:start] + replacement + text[end:], start
        relative = posixpath.relpath(helper.name, posixpath.dirname(by_name[name].relative_path))
        text = f"import * as TS_LOCAL from '{relative if relative.startswith('.') else './' + relative}';\n" + text
        _ = (directory / by_name[name].relative_path).write_text(text, encoding="utf-8")
    return {"turn_consumer": consumer_module, "turn_component": component_module,
            "response_parse": parse_module,
            **{f"bridge_{key}": value for key, value in sorted(bound.items())}}


def _compile_helpers(output: Path, staging: Path,
                     natives: BunNatives) -> tuple[Path, list[dict[str, str | int]]]:
    compiler = shlex.split(os.environ.get("CC", "cc"))
    if not compiler:
        raise ValueError("CC must name a compiler")
    launcher = staging / "claude"
    subprocess.run([*compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "launcher.c"), "-o", str(launcher)], check=True)
    prefixes = [value for value in (os.environ.get("PREFIX"), os.environ.get("CONDA_PREFIX"),
                                    sys.prefix) if value]
    include = next((Path(prefix) / "include/node" for prefix in prefixes
                    if (Path(prefix) / "include/node/node_api.h").is_file()), None)
    if include is None:
        raise FileNotFoundError("Node's node_api.h is required to build libwire.node")
    _ = (staging / "bun_natives.h").write_text(_natives_header(output / "bun", natives), encoding="utf-8")
    subprocess.run([*compiler, "-std=c11", "-shared", "-fPIC", "-O2", "-I", str(include), "-I", str(staging),
                    str(ROOT / "native_cache.c"), "-ldl", "-o", str(output / "libwire.node")], check=True)
    shim = staging / "utf8-shim.o"
    subprocess.run([*compiler, "-std=c11", "-O2", "-c", "-DTASKSOLVER_BUN_UTF8_SHIM", "-I", str(staging),
                    "-fno-pic", "-fno-pie", "-fno-stack-protector", "-fno-asynchronous-unwind-tables",
                    "-fno-unwind-tables", str(ROOT / "native_cache.c"), "-o", str(shim)], check=True)
    with shim.open("rb") as stream:
        elf = ELFFile(stream)
        section = elf.get_section_by_name(".text.wire_bun_clone_utf8")
        if section is None or any(item["sh_type"] in ("SHT_REL", "SHT_RELA") and item["sh_size"]
                                  for item in elf.iter_sections()):
            raise ValueError("UTF-8 shim must be self-contained native code without relocations")
        patches = _append_utf8_shim(output / "bun", section.data(), natives)
    shutil.copyfile(ROOT / "cache.mjs", output / "cache.mjs")
    return launcher, patches


def _generate_caches(output: Path, staging: Path) -> dict[str, str]:
    source = output / "source"
    paths = sorted(path for path in source.rglob("*") if path.is_file() and path.suffix != ".cjs")
    jobs = staging / "cache-jobs.json"
    _ = jobs.write_text(json.dumps([str(path) for path in paths]), encoding="utf-8")
    try:
        subprocess.run([str(output / "bun"), "--no-env-file", "--config=/dev/null",
                        "--tsconfig-override=/dev/null", "--no-install", str(output / "cache.mjs"),
                        str(output / "libwire.node"), str(jobs)], check=True, capture_output=True,
                       text=True, timeout=180)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Claude bytecode generation failed: {cast(object, error.stderr)}") from error
    hashes: dict[str, str] = {}
    for path in paths:
        cache = path.with_name(path.name + ".jsc")
        if not cache.is_file():
            raise ValueError(f"No fresh bytecode cache was generated for {path.name}")
        hashes[cache.relative_to(source).as_posix()] = _digest(cache)
    return hashes


def _tree_hashes(directory: Path, *, skip_cache: bool = False) -> dict[str, str]:
    return {path.relative_to(directory).as_posix(): _digest(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file() and not (skip_cache and path.suffix == ".jsc")}


def unbundle(output: Path, *, binary: Path | None = None, bun_graph: str = "bun_graph",
             pin: CLIPin | None = None) -> UnbundleResult:
    """Build a separate editable runtime; refuse any existing output path.

    ``pin`` permits explicit alternate verified fixtures/builds. By default the
    repository's vendor.json is authoritative. No binary is downloaded here.
    """
    selected_pin = pin if pin is not None else _load_pin(ROOT / "vendor.json")
    source = binary or ROOT / "build/cache" / f"claude-{selected_pin['sha256']}"
    if not matches(source, selected_pin):
        raise ValueError("Claude executable does not match the pinned SHA256 and size")
    executable = shutil.which(bun_graph)
    if executable is None:
        raise FileNotFoundError(f"bun_graph executable not found: {bun_graph}")
    graph = _read_graph(source)
    # bun_graph bakes its extraction directory into source string literals.
    output = Path(os.path.abspath(output))
    if any(character in str(output) for character in ('"', "'", "\\", "`", "$")) or any(
            ord(character) < 32 or ord(character) == 127 for character in str(output)):
        raise ValueError("Output path contains unsupported JavaScript path characters")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)  # FileExistsError also rejects dangling symlinks.
    try:
        staging = output / ".build"
        staging.mkdir(mode=0o700)
        extracted = staging / "extracted"
        extracted.mkdir()
        # Without the packed, shared bytecode, which the runtime regenerates from the edited source.
        completed = _bun_graph(executable, [str(source), "--extract", str(extracted), "--no-bytecode"], timeout=300)
        _ = (output / "bun-graph.txt").write_text(completed.stdout, encoding="utf-8")
        module_metadata = _prepare_code(extracted, output, graph)
        directory, assets, modules = (output / name for name in ("source", "assets", "modules"))
        source_contracts = _install_loader(directory, graph)
        source_contracts |= _patch_native_bridge(directory, graph)
        natives = _read_natives(source, executable)
        entrypoint = directory / graph.entry.relative_path
        bun = output / "bun"
        _clean_runtime(source, bun, executable)
        candidate, native_patches = _compile_helpers(output, staging, natives)
        caches = _generate_caches(output, staging)
        hashes = _tree_hashes(directory, skip_cache=True)
        runtime = output / "claude"
        manifest = output / "manifest.json"
        document: dict[str, object] = {
            "format": 3, "cli_version": selected_pin["version"],
            "original_sha256": selected_pin["sha256"],
            "runtime": "claude", "runtime_sha256": _digest(candidate),
            "bun": "bun", "bun_sha256": _digest(bun),
            "entrypoint": entrypoint.relative_to(output).as_posix(), "code_count": len(hashes),
            "source": "source", "assets": "assets", "modules": "modules",
            "code_sha256": hashes, "cache_sha256": caches,
            "asset_sha256": _tree_hashes(assets), "native_module_sha256": _tree_hashes(modules),
            "helper_sha256": {name: _digest(output / name) for name in ("libwire.node", "cache.mjs")},
            "module_metadata": module_metadata,
            "native_patches": native_patches,
            "bun_natives": asdict(natives),
            "source_contracts": source_contracts,
            "native_bridge": True,
        }
        _ = manifest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        candidate.rename(runtime)  # Publish an executable only after every check succeeds.
        shutil.rmtree(staging)
        return UnbundleResult(runtime, entrypoint, manifest, len(hashes), bun, directory, assets, modules)
    except BaseException:
        shutil.rmtree(output)
        raise


class _Arguments(argparse.Namespace):
    output: str = str(ROOT / "build/editable")
    binary: str | None = None
    bun_graph: str = "bun_graph"


def main() -> None:
    from .build_cli import fetch_cli

    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--output", default=str(ROOT / "build/editable"))
    _ = parser.add_argument("--binary", help="Pinned executable to copy (never modified)")
    _ = parser.add_argument("--bun-graph", default="bun_graph", help="bun_graph executable (default: PATH)")
    args = parser.parse_args(namespace=_Arguments())
    pin = _load_pin(ROOT / "vendor.json")
    binary = Path(args.binary) if args.binary else fetch_cli(ROOT, pin)
    result = unbundle(Path(args.output), binary=binary, bun_graph=args.bun_graph, pin=pin)
    print(result.runtime)


if __name__ == "__main__":
    main()
