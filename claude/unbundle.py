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
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, cast

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

from .build_cli import CLIPin, _load_pin, matches


ROOT: Final = Path(__file__).resolve().parent
TRAILER: Final = b"\n---- Bun! ----\n"
PREFIX: Final = "/$bunfs/"
RECORD_SIZE: Final = 52
SOURCE_HASHES: Final = 1 << 5
SUPPORTED_FLAGS: Final = (1 << 14) - 1


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


def _source_only(source: Path, target: Path, graph: BunGraph) -> None:
    """Prevent old bun_graph from expanding shared bytecode once per module."""
    shutil.copyfile(source, target)
    target.chmod(0o600)
    with target.open("r+b") as stream:
        for module in graph.modules:
            stream.seek(module.record_offset + 24)
            _ = stream.write(bytes(16))  # bytecode and module-info pointer pairs


def _clean_runtime(source: Path, target: Path) -> None:
    """Remove the application payload while retaining every native allocation."""
    objcopy = (os.environ.get("OBJCOPY") or shutil.which("x86_64-conda-linux-gnu-objcopy")
               or shutil.which("objcopy"))
    if objcopy is None:
        raise FileNotFoundError("GNU objcopy is required to extract the private Bun runtime")
    subprocess.run([objcopy, "--remove-section", ".bun", str(source), str(target)], check=True)
    target.chmod(0o755)
    # objcopy drops GNU_RELRO and the stack-size hint. Keep the original table,
    # changing only the LOAD sizes that removed the application payload.
    with source.open("rb") as original, target.open("r+b") as cleaned:
        old, new = ELFFile(original), ELFFile(cleaned)
        offset, count = cast(int, old["e_phoff"]), cast(int, old["e_phnum"])
        if old["e_phentsize"] != 56 or new["e_phoff"] != offset:
            raise ValueError("Unsupported ELF program-header layout")
        original.seek(offset)
        table = bytearray(original.read(count * 56))
        loads = {segment["p_vaddr"]: segment for segment in new.iter_segments()
                 if segment["p_type"] == "PT_LOAD"}
        application = old.get_section_by_name(".bun")
        if application is None:
            raise ValueError("Private Bun input is missing its application section")
        for index, segment in enumerate(old.iter_segments()):
            if segment["p_type"] == "PT_LOAD":
                after = loads.get(segment["p_vaddr"])
                if after is None or any(after[key] != segment[key] for key in (
                        "p_offset", "p_vaddr", "p_paddr", "p_flags", "p_align")):
                    raise ValueError("objcopy changed a native LOAD mapping")
                contains_application = (segment["p_vaddr"] <= application["sh_addr"]
                                        < segment["p_vaddr"] + segment["p_memsz"])
                if not contains_application and any(after[key] != segment[key] for key in (
                        "p_filesz", "p_memsz")):
                    raise ValueError("objcopy resized a LOAD unrelated to the application")
                struct.pack_into("<QQ", table, index * 56 + 32, after["p_filesz"], after["p_memsz"])
        cleaned.seek(offset)
        _ = cleaned.write(table)
        cleaned.seek(56)
        _ = cleaned.write(struct.pack("<H", count))
    with source.open("rb") as original, target.open("rb") as cleaned:
        old, new = ELFFile(original), ELFFile(cleaned)
        bun, data = old.get_section_by_name(".bun"), old.get_section_by_name(".data")
        if bun is None or data is None or new.get_section_by_name(".bun") is not None:
            raise ValueError("Unexpected clean Bun ELF layout")
        if new["e_entry"] != old["e_entry"]:
            raise ValueError("objcopy changed the private Bun entry point")
        for section in old.iter_sections():
            if not section["sh_flags"] & 2 or section.name == ".bun":
                continue
            after = new.get_section_by_name(section.name)
            if (after is None or after["sh_addr"] != section["sh_addr"]
                    or after["sh_offset"] != section["sh_offset"]
                    or after.data() != section.data()):
                raise ValueError(f"objcopy changed native section {section.name}")
        # The pinned ELF stores BUN_COMPILED.size in the padding after .data.
        # Removing .bun must clear it and remove the old payload's mapping.
        marker = data["sh_offset"] + data["sh_size"]
        original.seek(marker)
        cleaned.seek(marker)
        if (int.from_bytes(original.read(8), "little") != bun["sh_addr"]
                or cleaned.read(8) != bytes(8)):
            raise ValueError("Private Bun standalone-graph pointer was not cleared")
        for segment in new.iter_segments():
            if (segment["p_type"] == "PT_LOAD"
                    and segment["p_vaddr"] <= bun["sh_addr"]
                    < segment["p_vaddr"] + segment["p_memsz"]):
                raise ValueError("Clean Bun still maps the removed application payload")


def _append_utf8_shim(runtime: Path, code: bytes) -> list[dict[str, str | int]]:
    """Append compiled code without moving native sections or rewriting dynamic metadata."""
    if not code or len(code) > 4096:
        raise ValueError("Unexpected UTF-8 loader shim size")
    call_address = 0x69930AB
    original_call = bytes.fromhex("e8f09f4d01")
    with runtime.open("r+b") as stream:
        elf = ELFFile(stream)
        if (elf["e_type"] != "ET_EXEC" or elf["e_phentsize"] != 56
                or elf["e_shentsize"] != 64 or elf.get_section_by_name(".tasksolver.utf8") is not None):
            raise ValueError("Unsupported private Bun ELF layout for UTF-8 shim")
        phoff, phnum = cast(int, elf["e_phoff"]), cast(int, elf["e_phnum"])
        shoff, shnum = cast(int, elf["e_shoff"]), cast(int, elf["e_shnum"])
        string_index = cast(int, elf["e_shstrndx"])
        strings = elf.get_section(string_index).data()
        offsets = list(elf.address_offsets(call_address, 5))
        if len(offsets) != 1:
            raise ValueError("Private Bun source-loader call is not mapped uniquely")
        call_offset = offsets[0]
        stream.seek(call_offset)
        if stream.read(5) != original_call:
            raise ValueError("Private Bun source-loader call signature changed")
        stream.seek(phoff)
        headers = bytearray(stream.read(phnum * 56))
        stream.seek(shoff)
        sections = bytearray(stream.read(shnum * 64))
        end_address = max(segment["p_vaddr"] + segment["p_memsz"]
                          for segment in elf.iter_segments() if segment["p_type"] == "PT_LOAD")
        # Leave the former .bun start unmapped. BUN_COMPILED.size is already zero.
        address = (end_address + 4095) // 4096 * 4096 + 4096
        offset = (stream.seek(0, os.SEEK_END) + 4095) // 4096 * 4096
        table_size = (phnum + 1) * 56
        code_start = (table_size + 15) // 16 * 16
        target = address + code_start
        replacement = b"\xe8" + struct.pack("<i", target - call_address - 5)
        phdr_found = False
        for index, segment in enumerate(elf.iter_segments()):
            if segment["p_type"] == "PT_PHDR":
                headers[index * 56:(index + 1) * 56] = struct.pack(
                    "<IIQQQQQQ", 6, 4, offset, address, address, table_size, table_size, 8)
                phdr_found = True
        if not phdr_found:
            raise ValueError("Private Bun is missing PT_PHDR")
        length = code_start + len(code)
        headers.extend(struct.pack("<IIQQQQQQ", 1, 5, offset, address, address, length, length, 4096))
        string_offset = offset + length
        name_offset = len(strings)
        strings += b".tasksolver.utf8\0"
        section_offset = (string_offset + len(strings) + 7) // 8 * 8
        struct.pack_into("<QQ", sections, string_index * 64 + 24, string_offset, len(strings))
        sections.extend(struct.pack("<IIQQQQIIQQ", name_offset, 1, 6, target,
                                    offset + code_start, len(code), 0, 0, 16, 0))
        stream.seek(offset)
        _ = stream.write(headers)
        stream.seek(offset + code_start)
        _ = stream.write(code)
        stream.seek(string_offset)
        _ = stream.write(strings)
        stream.seek(section_offset)
        _ = stream.write(sections)
        stream.seek(32)
        _ = stream.write(struct.pack("<QQ", offset, section_offset))
        stream.seek(56)
        _ = stream.write(struct.pack("<H", phnum + 1))
        stream.seek(60)
        _ = stream.write(struct.pack("<H", shnum + 1))
        stream.seek(call_offset)
        _ = stream.write(replacement)
    return [{"name": "external_source_utf8", "address": call_address,
             "original": original_call.hex(), "replacement": replacement.hex(),
             "target": target, "section": ".tasksolver.utf8"}]


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


def _install_loader(directory: Path, graph: BunGraph) -> None:
    """Resolve extracted modules and filesystem assets relative to the installation."""
    code_names = {module.name for module in graph.modules if module.loader == 1}
    by_name = {module.name: module for module in graph.modules}
    worker = PREFIX + "root/src/plugins/functionHooks/hooks-worker/hooks-worker.js"
    pattern = re.compile(r'''(["'])(/\$bunfs/[^"'\n]+)\1''')
    for module in graph.modules:
        if module.loader != 1:
            continue
        path = directory / module.relative_path
        text = path.read_text(encoding="utf-8")

        def relocate(match: re.Match[str]) -> str:
            name = match.group(2)
            target = by_name.get(name)
            if target is None:
                return match.group()
            before = text[max(0, match.start() - 30):match.start()]
            if before.endswith(("from", "import", "import(", ".require(", "Be(")):
                return match.group()
            if target.loader != 5 and name != worker:
                raise ValueError(f"Unsupported non-import JavaScript path in {module.name}: {name}")
            physical = ("../assets/" if target.loader == 5 else "") + target.relative_path
            relative = posixpath.relpath(physical,
                                        posixpath.dirname(module.relative_path))
            return f"Bun.fileURLToPath(new URL({json.dumps('./' + relative)},import.meta.url))"

        text = pattern.sub(relocate, text)
        _ = path.write_text(text, encoding="utf-8")
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
    native_format = directory / "root/chunk-cgr0tezp.js"
    text = native_format.read_text(encoding="utf-8")
    before = 'function sp(){return typeof Bun<"u"&&Bun.isStandaloneExecutable===!0}'
    if text.count(before) != 1:
        raise ValueError("Pinned native-package classifier did not match exactly once")
    _ = native_format.write_text(text.replace(before, "function sp(){return!0}"), encoding="utf-8")


def _patch_native_bridge(directory: Path, version: str) -> None:
    """Attach to the native prompt queue and tee its sole SDK-event consumer."""
    if version != "2.1.316":
        raise ValueError("Native terminal bridge only supports Claude Code 2.1.316")
    relative = "root/chunk-18hwfzwa.js"
    module = directory / relative
    text = module.read_text(encoding="utf-8")
    mount = (
        "let tsQueue=Nu();P(()=>TS_LOCAL.attach({engine:h._engine,get sessionId(){return ze.id},"
        "enqueue:(frame)=>tsQueue.enqueue({value:frame.message.content,mode:\"prompt\","
        "agentId:et(),uuid:frame.uuid,turnAttributionKey:frame.uuid,"
        "origin:{kind:\"human\"},skipSlashCommands:!0}),"
        "isBusy:()=>h.guard.isActive||h.isExternalLoading,"
        "hasQueued:(uuid)=>tsQueue.getCommandQueue().some((command)=>command.uuid===uuid),"
        "onReady:(socketPath)=>h._requireHost().addNotification({key:\"tasksolver-local-bridge-ready\","
        "kind:\"feedback\",text:\"Python SDK socket: \"+socketPath,priority:\"immediate\",timeoutMs:15000}),"
        "onError:(message)=>h._requireHost().addNotification({key:\"tasksolver-local-bridge-error\","
        "kind:\"warning\",text:message,priority:\"immediate\"}),"
        "interrupt:()=>h.cancel(\"remote\")}),[h,ze.id,tsQueue]);"
    )
    edits = (
        ("return wvt(Xe,x,At,rt,kt,()=>", mount + "return wvt(Xe,x,At,rt,kt,()=>"),
        ('x.input.enqueue({type:"user",message:We?.message??{role:"user",content:""},parent_tool_use_id:null});',
         'x.input.enqueue({type:"user",message:We?.message??{role:"user",content:""},parent_tool_use_id:null,uuid:TS_LOCAL.userUuid(h,We?.uuid)});'),
        ("let kt;try{Be?.();while(!0){let{value:At,done:It}=await h.next();if(It)break;",
         "let kt;try{TS_LOCAL.begin(h,We?.uuid);Be?.();while(!0){let{value:At,done:It}=await h.next();if(It)break;TS_LOCAL.emit(h,We?.uuid,At);"),
        ("finally{Xe()}return kt}function SIe(",
         "finally{TS_LOCAL.finish(h,We?.uuid);Xe()}return kt}function SIe("),
    )
    for index, (before, after) in enumerate(edits):
        if text.count(before) != 1:
            raise ValueError(f"Native bridge source contract {index + 1} did not match exactly once")
        text = text.replace(before, after)
    helper = directory / "tasksolver-native-bridge.mjs"
    shutil.copyfile(ROOT / "native_bridge.mjs", helper)
    text = "import * as TS_LOCAL from '../tasksolver-native-bridge.mjs';\n" + text
    _ = module.write_text(text, encoding="utf-8")


def _compile_helpers(output: Path, staging: Path) -> tuple[Path, list[dict[str, str | int]]]:
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
    subprocess.run([*compiler, "-std=c11", "-shared", "-fPIC", "-O2", "-I", str(include),
                    str(ROOT / "native_cache.c"), "-ldl", "-o", str(output / "libwire.node")], check=True)
    shim = staging / "utf8-shim.o"
    subprocess.run([*compiler, "-std=c11", "-O2", "-c", "-DTASKSOLVER_BUN_UTF8_SHIM",
                    "-fno-pic", "-fno-pie", "-fno-stack-protector", "-fno-asynchronous-unwind-tables",
                    "-fno-unwind-tables", str(ROOT / "native_cache.c"), "-o", str(shim)], check=True)
    with shim.open("rb") as stream:
        elf = ELFFile(stream)
        section = elf.get_section_by_name(".text.wire_bun_clone_utf8")
        if section is None or any(item["sh_type"] in ("SHT_REL", "SHT_RELA") and item["sh_size"]
                                  for item in elf.iter_sections()):
            raise ValueError("UTF-8 shim must be self-contained native code without relocations")
        patches = _append_utf8_shim(output / "bun", section.data())
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
        stripped = staging / "source-only"
        _source_only(source, stripped, graph)
        extracted = staging / "extracted"
        extracted.mkdir()
        completed = subprocess.run([executable, str(stripped), "--extract", str(extracted)],
                                   check=True, capture_output=True, text=True, timeout=120)
        _ = (output / "bun-graph.txt").write_text(completed.stdout, encoding="utf-8")
        stripped.unlink()
        module_metadata = _prepare_code(extracted, output, graph)
        directory, assets, modules = (output / name for name in ("source", "assets", "modules"))
        _install_loader(directory, graph)
        _patch_native_bridge(directory, selected_pin["version"])
        entrypoint = directory / graph.entry.relative_path
        bun = output / "bun"
        _clean_runtime(source, bun)
        candidate, native_patches = _compile_helpers(output, staging)
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
    bun_graph: str | None = None


def main() -> None:
    from .build_cli import build_bun_graph, fetch_cli

    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--output", default=str(ROOT / "build/editable"))
    _ = parser.add_argument("--binary", help="Pinned executable to copy (never modified)")
    _ = parser.add_argument("--bun-graph", help="Override the checksum-pinned build extractor")
    args = parser.parse_args(namespace=_Arguments())
    pin = _load_pin(ROOT / "vendor.json")
    binary = Path(args.binary) if args.binary else fetch_cli(ROOT, pin)
    tool = args.bun_graph or str(build_bun_graph(ROOT))
    result = unbundle(Path(args.output), binary=binary, bun_graph=tool, pin=pin)
    print(result.runtime)


if __name__ == "__main__":
    main()
