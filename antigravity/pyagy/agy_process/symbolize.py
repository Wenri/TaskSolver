"""Symbolize AGY_PROC_STACK `callstack` events against the full funcmap.

The shim emits each stack frame as a **link vaddr** (`pc - module_base`);
`build_symbols.py` writes `symbols/funcmap.tsv.gz` (`addr<TAB>name`, sorted by addr,
keyed by the same link vaddr `= text_base + entryoff`). So symbolizing a frame is a
plain `bisect` — no runtime base needed. Stdlib only (kept out of the embedded
interpreter's import path; this is a user-side analysis tool).

A `callstack` event is `{"kind":"callstack","src":<hook kind>,"frames":[vaddr,...]}`.
`frames[0]` is the return address into the hook's caller, `frames[1]` its caller, …
For **gum** hooks (tls_write/decrypt) the chain is complete from the target upward.
For **trampoline** hooks the captured rbp is the *caller's* frame, so the immediate
caller is implicit — the chain starts one frame above `src`.

CLI:
    python3 -m pyagy.agy_process.symbolize <capture.jsonl> [funcmap.tsv.gz] [--graph]
"""
import bisect
import collections
import gzip
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# pyagy/agy_process/ -> pyagy -> antigravity ; funcmap lives at antigravity/symbols/
DEFAULT_FUNCMAP = os.path.join(os.path.dirname(os.path.dirname(_HERE)),
                               "symbols", "funcmap.tsv.gz")


class Symbolizer:
    """addr→name lookup over the sorted funcmap (bisect on the enclosing entry)."""

    def __init__(self, funcmap_path=None):
        path = funcmap_path or DEFAULT_FUNCMAP
        self.addrs, self.names = [], []
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                a, _, nm = line.rstrip("\n").partition("\t")
                if not a:
                    continue
                self.addrs.append(int(a, 16))
                self.names.append(nm)
        # funcmap is written sorted, but don't trust it blindly.
        if any(self.addrs[i] > self.addrs[i + 1] for i in range(len(self.addrs) - 1)):
            order = sorted(range(len(self.addrs)), key=lambda i: self.addrs[i])
            self.addrs = [self.addrs[i] for i in order]
            self.names = [self.names[i] for i in order]

    def name(self, pc):
        i = bisect.bisect_right(self.addrs, pc) - 1
        return self.names[i] if i >= 0 else f"?{pc:#x}"


def load_callstacks(capture_path):
    with open(capture_path) as f:
        for line in f:
            line = line.strip()
            if not line or '"callstack"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("kind") == "callstack":
                yield o


def render_stacks(capture_path, sym, top=12):
    """Group identical (src, symbolized-stack) and print with fire counts."""
    groups = collections.defaultdict(collections.Counter)
    for ev in load_callstacks(capture_path):
        names = tuple(sym.name(pc) for pc in ev.get("frames", []))
        groups[ev.get("src", "?")][names] += 1
    out = []
    for src in sorted(groups):
        stacks = groups[src]
        out.append(f"=== {src}: {sum(stacks.values())} fire(s), "
                   f"{len(stacks)} distinct stack(s) ===")
        for stack, c in stacks.most_common(top):
            out.append(f"  [{c}x] {src}")
            for nm in stack:
                out.append(f"        <- {nm}")
    return "\n".join(out)


def call_graph(capture_path, sym):
    """Aggregate caller→callee edges (chain = src leaf, then callers outward)."""
    edges = collections.Counter()
    for ev in load_callstacks(capture_path):
        chain = [ev.get("src", "?")] + [sym.name(pc) for pc in ev.get("frames", [])]
        for i in range(len(chain) - 1):
            callee, caller = chain[i], chain[i + 1]
            edges[(caller, callee)] += 1
    return edges


def render_graph(capture_path, sym, top=60):
    edges = call_graph(capture_path, sym)
    out = [f"=== call graph: {len(edges)} distinct caller->callee edges ==="]
    for (caller, callee), c in edges.most_common(top):
        out.append(f"  [{c:4d}x] {caller}  ->  {callee}")
    return "\n".join(out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    graph = "--graph" in argv
    argv = [a for a in argv if a != "--graph"]
    if not argv:
        print("usage: symbolize.py <capture.jsonl> [funcmap.tsv.gz] [--graph]",
              file=sys.stderr)
        return 2
    capture = argv[0]
    funcmap = argv[1] if len(argv) > 1 else None
    sym = Symbolizer(funcmap)
    print(render_graph(capture, sym) if graph else render_stacks(capture, sym))
    return 0


if __name__ == "__main__":
    sys.exit(main())
