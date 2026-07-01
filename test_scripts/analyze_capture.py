#!/usr/bin/env python3
"""Summarize an agy-capture.jsonl for plotting/analysis.

    python3 analyze_capture.py agy-capture.jsonl [--plot out.png]

Prints per-kind counts and byte totals, per-connection request/response sizes,
and (with --plot, needs matplotlib) a cumulative-bytes-over-time chart split by
direction — the basic view for "how much did agy send/receive, and when".
"""
import argparse
import collections
import json


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--plot", metavar="PNG", help="write a cumulative-bytes chart")
    args = ap.parse_args()

    rows = load(args.capture)
    if not rows:
        print("no events")
        return
    t0 = min(r["t"] for r in rows if "t" in r)

    by_kind = collections.Counter()
    bytes_kind = collections.Counter()
    per_conn = collections.defaultdict(lambda: collections.Counter())
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

    h2 = [r for r in rows if r.get("kind") == "h2msg"]
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
        fig, ax = plt.subplots(figsize=(10, 5))
        for direction, kinds in (("sent", {"tls_write"}), ("recv", {"tls_read"})):
            pts = sorted((r["t"] - t0, r.get("len", 0)) for r in rows if r.get("kind") in kinds)
            if not pts:
                continue
            xs, cum, tot = [], [], 0
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
