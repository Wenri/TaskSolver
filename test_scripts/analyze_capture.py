#!/usr/bin/env python3
"""Summarize an agy-capture.jsonl for plotting/analysis.

    python3 analyze_capture.py agy-capture.jsonl [--plot out.png]

Prints per-kind counts and byte totals, per-connection request/response sizes,
and (with --plot, needs matplotlib) a cumulative-bytes-over-time chart split by
direction — the basic view for "how much did agy send/receive, and when".
"""

from __future__ import annotations

import argparse
import collections
from typing import NotRequired, Protocol, TypedDict, cast

from wirecap.types import json_loads_object


class CaptureRecord(TypedDict):
    t: float
    kind: str
    len: NotRequired[int]
    body_len: NotRequired[int]
    stream: NotRequired[int]


class H2Record(TypedDict):
    t: float
    kind: str
    headers: dict[str, str]
    conn: int
    dir: str
    h2sid: int
    body_len: int


def load(path: str) -> list[CaptureRecord]:
    rows: list[CaptureRecord] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(cast(CaptureRecord, json_loads_object(line)))
                except Exception:
                    pass
    return rows



class PlotAxes(Protocol):
    def plot(self, x: list[float], y: list[int], *, label: str) -> object: ...
    def set_xlabel(self, label: str) -> object: ...
    def set_ylabel(self, label: str) -> object: ...
    def legend(self) -> object: ...
    def set_title(self, title: str) -> object: ...


class PlotFigure(Protocol):
    def tight_layout(self) -> None: ...
    def savefig(self, path: str) -> None: ...


class Arguments(argparse.Namespace):
    capture: str
    plot: str | None

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--plot", metavar="PNG", help="write a cumulative-bytes chart")
    args = ap.parse_args(namespace=Arguments())

    rows = load(args.capture)
    if not rows:
        print("no events")
        return
    t0 = min(r["t"] for r in rows if "t" in r)

    by_kind: collections.Counter[str] = collections.Counter()
    bytes_kind: collections.Counter[str] = collections.Counter()
    per_conn: collections.defaultdict[int, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for r in rows:
        k = r.get("kind", "?")
        by_kind[k] += 1
        bytes_kind[k] += r.get("len", r.get("body_len", 0))
        if "stream" in r:
            per_conn[r["stream"]][k] += r.get("len", 0)

    print(f"events: {len(rows)}   window: {max(r['t'] for r in rows if 't' in r) - t0:.2f}s\n")
    print(f"{'kind':<12}{'count':>8}{'bytes':>14}")
    for k in sorted(by_kind):
        print(f"{k:<12}{by_kind[k]:>8}{bytes_kind[k]:>14}")

    h2 = [cast(H2Record, r) for r in rows if r["kind"] == "h2msg"]
    if h2:
        print(f"\nHTTP/2 messages: {len(h2)}")
        for m in h2[:20]:
            path = m.get("headers", {}).get(":path") or m.get("headers", {}).get(":status", "")
            print(f"  conn={m['conn']:#x} {m['dir']} sid={m['h2sid']} "
                  f"body={m['body_len']}  {path}")

    if args.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception as e:
            print(f"\n--plot needs matplotlib: {e}")
            return
        fig, ax = cast(tuple[PlotFigure, PlotAxes], plt.subplots(figsize=(10, 5)))
        for direction, kinds in (("sent", {"tls_write"}), ("recv", {"tls_read"})):
            pts = sorted((r["t"] - t0, r.get("len", 0)) for r in rows if r.get("kind") in kinds)
            if not pts:
                continue
            xs: list[float] = []
            cum: list[int] = []
            tot = 0
            for t, n in pts:
                tot += n
                xs.append(t)
                cum.append(tot)
            ax.plot(xs, cum, label=f"{direction} (cum bytes)")
        ax.set_xlabel("seconds since first event")
        ax.set_ylabel("cumulative bytes")
        ax.legend()
        ax.set_title("agy TLS traffic over time")
        fig.tight_layout()
        fig.savefig(args.plot)
        print(f"\nwrote {args.plot}")


if __name__ == "__main__":
    main()
