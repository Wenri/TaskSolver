"""Render a time-ordered RPC trace from a capture.

The shim trampolines agy's `(*CodeAssistClient).*` methods — one hook per backend
RPC — emitting an `rpc_<name>` event when each fires. This tool prints them as a
labeled, time-ordered timeline of a turn (the app-level view that sits alongside the
wire `genai_turn` decode), optionally enriched with:
  * the call stack per RPC  (run with AGY_PROC_STACK=1 → `callstack` events)
  * the request proto walked (run with AGY_PROC_CGT_ARGS=1 → `cgt_args` reports)
It also folds in `genai_turn` (the decoded model turn) and `proto_marshal`/`tls_write`
egress if those kinds are present, so a combined capture reads as one timeline.

Stdlib only. CLI:
    python3 -m pyagy.agy_process.rpctrace <capture.jsonl> [funcmap.tsv.gz] [--stacks]
"""
import json
import sys

# rpc_<kind> -> readable RPC name (mirrors the CodeAssistClient hooks in proc.def).
RPC_KINDS = {
    "rpc_stream_generate": "StreamGenerateContent  (the model turn)",
    "rpc_generate": "GenerateContent",
    "rpc_load_code_assist": "FetchLoadCodeAssistResponse",
    "rpc_fetch_userinfo": "FetchUserInfo",
    "rpc_fetch_models": "FetchAvailableModels",
    "rpc_list_experiments": "ListExperiments",
    "rpc_quota": "RetrieveUserQuotaSummary",
    "rpc_record_offered": "RecordConversationOffered",
    "rpc_record_trajectory": "RecordTrajectorySegmentAnalytics",
    "rpc_write_acls": "WriteTrajectoryACLs",
}
# other kinds worth showing inline on the timeline
CONTEXT_KINDS = {
    "genai_turn": "· model turn decoded (http1sse)",
    "proto_marshal": "· proto.Marshal (egress request bytes)",
    "send_user_msg": "· SendUserMessage (app boundary)",
}


def _events(capture_path):
    with open(capture_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if "t" in o:
                yield o


def trace(capture_path, funcmap=None, with_stacks=False):
    # optional stack enrichment: map (src rpc kind) -> the first symbolized stack seen
    stacks = {}
    if with_stacks:
        try:
            from .symbolize import Symbolizer
            sym = Symbolizer(funcmap)
            for o in _events(capture_path):
                if o.get("kind") == "callstack" and o.get("src", "").startswith("rpc_"):
                    stacks.setdefault(o["src"], [sym.name(pc) for pc in o.get("frames", [])])
        except Exception as e:
            print(f"(stack enrichment unavailable: {e})", file=sys.stderr)

    rows = []
    t0 = None
    counts = {}
    for o in _events(capture_path):
        k = o.get("kind")
        if k in RPC_KINDS:
            label = "RPC  " + RPC_KINDS[k]
        elif k in CONTEXT_KINDS:
            label = CONTEXT_KINDS[k]
        else:
            continue
        counts[k] = counts.get(k, 0) + 1
        rows.append((o["t"], k, label))

    rows.sort(key=lambda r: r[0])            # authoritative fire order (concurrent goroutines)
    t0 = rows[0][0] if rows else 0
    rows = [(t - t0, k, label) for t, k, label in rows]

    out = [f"=== RPC trace: {len(rows)} events over {rows[-1][0]:.2f}s ==="
           if rows else "=== RPC trace: no rpc_*/context events in this capture ==="]
    for dt, k, label in rows:
        out.append(f"  +{dt:7.3f}s  {label}")
        if k in stacks:
            for nm in stacks[k][:14]:
                out.append(f"              <- {nm}")
    if counts:
        out.append("\n  counts: " + "  ".join(f"{RPC_KINDS.get(k,k).split(' ')[0]}={n}"
                                               for k, n in sorted(counts.items())))
    return "\n".join(out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    with_stacks = "--stacks" in argv
    argv = [a for a in argv if a != "--stacks"]
    if not argv:
        print("usage: rpctrace.py <capture.jsonl> [funcmap.tsv.gz] [--stacks]", file=sys.stderr)
        return 2
    print(trace(argv[0], argv[1] if len(argv) > 1 else None, with_stacks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
