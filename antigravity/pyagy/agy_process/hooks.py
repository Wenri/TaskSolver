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
    # stages 11-12: model-text pipeline probe (diagnostic/RE only — the response data
    # path stays on the wire via http1sse; see README "App-boundary text probe").
    {"id": "GET_DELTA_CCPA",
     "symbol": "…api_server_go_proto.(*GetChatMessageResponse).GetDeltaText",
     "mode": "async", "kind": "delta_ccpa", "stage": 11, "leave": True,
     "note": "leaf getter, returns delta text (on_leave); inactive ccpa provider — doesn't fire on 1.0.16"},
    {"id": "GET_DELTA_CMPL",
     "symbol": "…codeium_common_go_proto.(*CompletionDelta).GetDeltaText",
     "mode": "async", "kind": "delta_completion", "stage": 11, "leave": True,
     "note": "leaf getter, returns delta text (on_leave); inactive provider — doesn't fire"},
    {"id": "FH_FINALIZE",
     "symbol": "…generator.(*streamResponseHandler).finalizePlannerResponse",
     "mode": "async", "kind": "fh_finalize", "stage": 12, "leave": False,
     "note": "framework choke point (trampoline); fires, but output text is built during the call → not in entry args"},
    {"id": "FH_UPDATE",
     "symbol": "…generator.(*streamResponseHandler).updateWithStep",
     "mode": "async", "kind": "fh_update", "stage": 12, "leave": False,
     "note": "framework per-step (trampoline); input context only in entry args"},
    {"id": "FH_PROCESS",
     "symbol": "…generator.(*streamResponseHandler).processStream",
     "mode": "async", "kind": "fh_process", "stage": 12, "leave": False,
     "note": "framework stream consumer (trampoline)"},
    {"id": "CORE_PLANSTEP",
     "symbol": "…core.createPlannerResponseStep",
     "mode": "async", "kind": "core_planstep", "stage": 12, "leave": False,
     "note": "builds assistant Step (trampoline); inlined/off-path — fired 0× in probe"},
    # stage 13: CodeAssistClient RPC trace (trampoline; kind = RPC label). The
    # app-semantic backend boundary — request via AGY_PROC_CGT_ARGS, stack via
    # AGY_PROC_STACK. StreamGenerateContent is the model turn. See rpctrace.py.
    {"id": "RPC_STREAM_GEN", "symbol": "…codeassistclient.(*CodeAssistClient).StreamGenerateContent",
     "mode": "async", "kind": "rpc_stream_generate", "stage": 13, "leave": False,
     "note": "the model turn (streaming); request proto at entry"},
    {"id": "RPC_GEN", "symbol": "…(*CodeAssistClient).GenerateContent",
     "mode": "async", "kind": "rpc_generate", "stage": 13, "leave": False, "note": "non-streaming generate"},
    {"id": "RPC_LOAD_CA", "symbol": "…(*CodeAssistClient).FetchLoadCodeAssistResponse",
     "mode": "async", "kind": "rpc_load_code_assist", "stage": 13, "leave": False, "note": "startup"},
    {"id": "RPC_USERINFO", "symbol": "…(*CodeAssistClient).FetchUserInfo",
     "mode": "async", "kind": "rpc_fetch_userinfo", "stage": 13, "leave": False, "note": "startup"},
    {"id": "RPC_MODELS", "symbol": "…(*CodeAssistClient).FetchAvailableModels",
     "mode": "async", "kind": "rpc_fetch_models", "stage": 13, "leave": False, "note": "MendelStateCache pollLoop"},
    {"id": "RPC_EXPERIMENTS", "symbol": "…(*CodeAssistClient).ListExperiments",
     "mode": "async", "kind": "rpc_list_experiments", "stage": 13, "leave": False, "note": "MendelStateCache pollLoop"},
    {"id": "RPC_QUOTA", "symbol": "…(*CodeAssistClient).RetrieveUserQuotaSummary",
     "mode": "async", "kind": "rpc_quota", "stage": 13, "leave": False, "note": "store quotaRefreshLoop"},
    {"id": "RPC_REC_OFFERED", "symbol": "…(*CodeAssistClient).RecordConversationOffered",
     "mode": "async", "kind": "rpc_record_offered", "stage": 13, "leave": False, "note": "telemetry"},
    {"id": "RPC_REC_TRAJ", "symbol": "…(*CodeAssistClient).RecordTrajectorySegmentAnalytics",
     "mode": "async", "kind": "rpc_record_trajectory", "stage": 13, "leave": False,
     "note": "post-turn telemetry (AgentExecutor.recordTelemetryAfterExecution)"},
    {"id": "RPC_WRITE_ACLS", "symbol": "…(*CodeAssistClient).WriteTrajectoryACLs",
     "mode": "async", "kind": "rpc_write_acls", "stage": 13, "leave": False, "note": "trajectory ACLs"},
]

# Kinds the correlator/decoder synthesize (not raw hooks) — emitted into the capture.
DERIVED_KINDS = {
    "genai_turn": "merged model request+response (capture.Correlator)",
    "h2msg": "reassembled HTTP/2 message (h2reassemble)",
    "rewrite_applied": "a SYNC egress rewrite was applied",
    "rewrite_skip": "a rewrite was skipped (length would change)",
    "rewrite_error": "a rewrite rule/func raised",
    "cgt_args": "AGY_PROC_CGT_ARGS diagnostic: a trampoline hook's arg-graph report",
    "callstack": "AGY_PROC_STACK diagnostic: symbolizable Go call stack at a hook fire",
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
