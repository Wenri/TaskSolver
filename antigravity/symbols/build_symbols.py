#!/usr/bin/env python3
"""Resolve BOOKS in an unpatched agy image and write symbols.json for the native host.

Active books must have an intact supported prologue and a verified entry GC map.
The original argument bitmap plus morestack spills distinguish saved register
pointers from true stack roots; no pointer masks are inferred from signatures.

Usage (from antigravity/):  python3 -m symbols.build_symbols <agy-binary> <out.json>
"""
import gzip
import json
import os
import sys
from collections.abc import Sequence

from .agyelf import Book, EntryGCMap, GoImage, SymbolsMetadata, active_rows, book_targets, load_procdef


def resolve_symbols(data: bytes, image_path: str, rows: Sequence[Book] | None = None
                    ) -> tuple[SymbolsMetadata, list[tuple[int, str]]]:
    """Resolve and validate an unpatched image without writing any output files."""
    rows = load_procdef() if rows is None else rows
    targets = book_targets(rows)
    installed = {r["symbol"] for r in active_rows(rows)}

    img = GoImage(data)
    name2addr: dict[str, int] = {}
    funcmap: list[tuple[int, str]] = []
    for addr, nm in img.functions():
        name2addr.setdefault(nm, addr)
        funcmap.append((addr, nm))

    books: dict[str, int] = {}
    skips: dict[str, int] = {}
    prologues: dict[str, str] = {}
    gc_maps: dict[str, EntryGCMap] = {}
    missing: list[str] = []
    unverified: list[str] = []
    for nm in targets:
        if nm not in name2addr:
            missing.append(nm)
            continue
        a = name2addr[nm]
        books[nm] = a
        skips[nm] = img.prologue_skip(a)
        prologues[nm] = img.frame_prologue_hex(a, skips[nm])
        if nm in installed and (skips[nm] <= 0 or len(prologues[nm]) not in (16, 22)):
            unverified.append(nm)

    if unverified or missing:
        raise SystemExit(f"FAILED verification: missing={missing} unverified={unverified}")

    for name in sorted(installed):
        try:
            gc_maps[name] = img.entry_gc_map(books[name])
        except (ValueError, KeyError) as exc:
            raise SystemExit(f"FAILED verification: entry GC map for {name}: {exc}") from exc
    try:
        planner_itab = img.planner_response_itab()
    except ValueError as exc:
        raise SystemExit(f"FAILED verification: planner response itab: {exc}") from exc

    out: SymbolsMetadata = {
        "agy_path": image_path,
        "build_id": img.elf.build_id(),
        "ptr_size": img.ptr_size,
        "moduledata_vaddr": img.md_vaddr,
        "text_base": img.text_base,
        "minpc": img.minpc,
        "total_funcs": img.nfunc,
        "books": books,
        "skips": skips,
        "prologues": prologues,
        "missing": missing,
        "gc_maps": gc_maps,
        "planner_response_itab": planner_itab,
    }
    return out, sorted(funcmap)


def write_symbols(metadata: SymbolsMetadata, funcmap: list[tuple[int, str]], outpath: str) -> None:
    """Write validated metadata and its sibling funcmap; conversion supplies staging paths."""
    with open(outpath, "w") as f:
        json.dump(metadata, f, indent=2)

    # Stack symbolization subtracts the load bias from captured PCs before searching this map.
    fmpath = os.path.join(os.path.dirname(os.path.abspath(outpath)), "funcmap.tsv.gz")
    with gzip.open(fmpath, "wt", encoding="utf-8") as f:
        for addr, nm in funcmap:
            f.write(f"{addr:x}\t{nm}\n")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python3 -m symbols.build_symbols <unpatched-agy-binary> <out.json>")
    binpath, outpath = sys.argv[1], sys.argv[2]
    rows = load_procdef()
    with open(binpath, "rb") as f:
        metadata, funcmap = resolve_symbols(f.read(), binpath, rows)
    write_symbols(metadata, funcmap, outpath)

    installed = {r["symbol"] for r in active_rows(rows)}
    books, skips = metadata["books"], metadata["skips"]
    fmpath = os.path.join(os.path.dirname(os.path.abspath(outpath)), "funcmap.tsv.gz")
    print(f"wrote {outpath}")
    print(f"  funcmap:    {fmpath} ({len(funcmap)} funcs)")
    print(f"  build-id:   {metadata['build_id']}")
    print(f"  moduledata: {metadata['moduledata_vaddr']:#x}   text_base: {metadata['text_base']:#x}"
          + (f"   minpc: {metadata['minpc']:#x}" if metadata['minpc'] else ""))
    print(f"  funcs:      {metadata['total_funcs']}")
    print(f"  books:      {len(books)}/{len(book_targets(rows))}")
    for nm in book_targets(rows):
        flag = "" if nm in installed else "  (retired)"
        print(f"    {books[nm]:#012x} (+{skips[nm]:2d})  {nm}{flag}")
    print(f"  all {len(installed)} installed book addresses verified as Go prologues ✓")


if __name__ == "__main__":
    main()
