"""Machine-readable mirror of the native hook table (src/proc.def).

`proc.def` is the source of truth (it feeds the C X-macro); this is the Python-side
copy so tooling and the client can reason about stages, kinds, and which hook is the
rewrite surface without parsing C. Keep the two in sync when you add/remove a hook.

Fields per entry: ``id`` (C enum tag), ``symbol`` (Go symbol hooked), ``mode``
(``async`` log-only / ``sync`` blocks for a modify verdict), ``kind`` (the string
passed to ``dispatch``), ``stage`` (which AGY_PROC_STAGE enables it), ``leave``
(intercepts the return value), ``note``.
"""

# The package's recommended stage: tls_write (egress, sync-capable) + decrypt
# (ingress) — enough to capture and rewrite the model turn, nothing that parks agy.
DEFAULT_STAGE = 3

HOOKS = [
    {"id": "SMOKE_GETENV", "symbol": "os.Getenv", "mode": "async", "kind": "smoke",
     "stage": 2, "leave": False, "note": "liveness smoke; fires often, no network"},
    {"id": "TLS_WRITE", "symbol": "crypto/tls.(*Conn).Write", "mode": "async",
     "kind": "tls_write", "stage": 3, "leave": False,
     "note": "egress c2s; the ONLY rewrite surface (AGY_PROC_TLS_WRITE_SYNC); "
             "carries the full HTTP/1.1 model request"},
    {"id": "H2_PIPE_WRITE", "symbol": "net/http/internal/http2.(*pipe).Write",
     "mode": "async", "kind": "resp", "stage": 5, "leave": False,
     "note": "parks under contention → reference only"},
    {"id": "TLS_DECRYPT", "symbol": "crypto/tls.(*halfConn).decrypt", "mode": "async",
     "kind": "tls_read", "stage": 3, "leave": True,
     "note": "ingress s2c (decrypted inbound records); carries the SSE response"},
    {"id": "TLS_READ", "symbol": "crypto/tls.(*Conn).Read", "mode": "async",
     "kind": "tls_read", "stage": 5, "leave": True, "note": "parks while hooked → STALL"},
    {"id": "HTTP_RT", "symbol": "net/http.(*Transport).RoundTrip", "mode": "async",
     "kind": "http_rt", "stage": 5, "leave": False, "note": "parks while hooked → STALL"},
    {"id": "SER_ROOT",
     "symbol": "google3/third_party/jetski/cli/model/model.(*RootModel).Serialize",
     "mode": "async", "kind": "serialize", "stage": 4, "leave": True,
     "note": "app-layer R&D; empirically does not fire for the model request"},
    {"id": "MAR_PROMPT",
     "symbol": "google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON",
     "mode": "async", "kind": "marshal", "stage": 4, "leave": True, "note": "app-layer R&D"},
    {"id": "PROTO_MARSHAL",
     "symbol": "google3/third_party/golang/gogo/protobuf/proto/proto.Marshal",
     "mode": "async", "kind": "proto_marshal", "stage": 4, "leave": True,
     "note": "app-layer R&D"},
    {"id": "CGT_SEND_USER_MSG",
     "symbol": "google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage",
     "mode": "async", "kind": "send_user_msg", "stage": 8, "leave": False,
     "note": "cgocall-trampoline app-boundary hook (observe)"},
    {"id": "CGT_STREAM_SEND",
     "symbol": "google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send",
     "mode": "async", "kind": "stream_send", "stage": 8, "leave": False,
     "note": "cgocall-trampoline app-boundary hook (observe)"},
    {"id": "CGT_GETENV", "symbol": "os.Getenv", "mode": "async", "kind": "cgt_getenv",
     "stage": 9, "leave": False, "note": "cgocall-trampoline mechanism validator"},
]

# Kinds the correlator/decoder synthesize (not raw hooks) — emitted into the capture.
DERIVED_KINDS = {
    "genai_turn": "merged model request+response (capture.Correlator)",
    "h2msg": "reassembled HTTP/2 message (h2reassemble)",
    "rewrite_applied": "a SYNC egress rewrite was applied",
    "rewrite_skip": "a rewrite was skipped (length would change)",
    "rewrite_error": "a rewrite rule/func raised",
}


def by_kind(kind):
    for h in HOOKS:
        if h["kind"] == kind:
            return h
    return None


def by_stage(stage):
    return [h for h in HOOKS if h["stage"] == stage]


def sync_capable():
    """Kinds that can rewrite egress (today: only tls_write)."""
    return [h["kind"] for h in HOOKS if h["kind"] == "tls_write"]
