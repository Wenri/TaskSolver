#!/usr/bin/env python3
"""Check that a built tasksolver .conda is a redistributable bundle.

    pixi run python tools/check_bundle.py dist/tasksolver-*.conda [--extract DIR]

The package must ship Codex and Kimi Code with their licenses, and none of the runtimes built from
proprietary executables (tasksolver.reconstruct.LOCAL_ONLY). With --extract the payload is also
unpacked into DIR, e.g. to try `pixi run reconstruct --target DIR/lib/python3.X/site-packages`.
Reads the .conda container (zip of zstd tarballs) with the standard library (Python >= 3.14).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
import zipfile
from compression import zstd
from pathlib import Path
from typing import Final, cast

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tasksolver.reconstruct import LOCAL_ONLY

REDISTRIBUTED: Final = ("pycodex/vendor/codex", "pykimi/vendor/main.mjs", "pykimi/vendor/search-worker.mjs",
                        "pykimi/vendor/wirecap_node.node")
LICENSES: Final = ("LICENSE", "codex/vendor/LICENSE", "codex/vendor/NOTICE", "kimi/vendor/kimi-code/LICENSE")


def _member(archive: zipfile.ZipFile, prefix: str) -> tarfile.TarFile:
    name = next((name for name in archive.namelist() if name.startswith(prefix)), None)
    if name is None:
        raise ValueError(f"{archive.filename} has no {prefix}*.tar.zst")
    return tarfile.open(fileobj=io.BytesIO(zstd.decompress(archive.read(name))))


def problems(package: Path, extract: Path | None = None) -> list[str]:
    """What keeps ``package`` from being a redistributable bundle (empty when it is one)."""
    with zipfile.ZipFile(package) as archive:
        with _member(archive, "info-") as info:
            listing = info.extractfile("info/paths.json")
            if listing is None:
                return ["info/paths.json is missing"]
            entries = cast(dict[str, list[dict[str, object]]], json.load(listing))["paths"]
        if extract is not None:
            with _member(archive, "pkg-") as payload:
                payload.extractall(extract, filter="tar")
    paths = [str(entry["_path"]) for entry in entries]
    site = next((path.split("/pyagy/")[0] for path in paths if "/pyagy/" in path), None)
    if site is None:
        return ["no pyagy package in the payload"]
    found: list[str] = []
    found += [f"ships {item}" for item in LOCAL_ONLY
              if any(path == f"{site}/{item}" or path.startswith(f"{site}/{item}/") for path in paths)]
    found += [f"lacks {item}" for item in REDISTRIBUTED if f"{site}/{item}" not in paths]
    found += [f"lacks the license file {item}" for item in LICENSES if not any(path.endswith(item) for path in paths)]
    return found


class _Arguments(argparse.Namespace):
    packages: list[Path] = []
    extract: Path | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    _ = parser.add_argument("packages", nargs="+", type=Path)
    _ = parser.add_argument("--extract", type=Path, metavar="DIR", help="also unpack the payload here")
    args = parser.parse_args(argv, namespace=_Arguments())
    status = 0
    for package in args.packages:
        found = problems(package, args.extract)
        print(f"{package.name}: " + ("redistributable" if not found else "; ".join(found)))
        status |= bool(found)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
