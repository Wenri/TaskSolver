"""Machine-readable mirror of the native hook table (src/procdef.h).

`procdef.h` is the source of truth (it feeds the C X-macro); this is the Python-side
copy so tooling and the client can reason about mechanisms, kinds, and which hook is the
rewrite surface without parsing C. Keep the two in sync when you add/remove a hook.

Fields per entry: ``id`` (C enum tag), ``symbol`` (Go symbol hooked), ``mode``
(``async`` log-only / ``sync`` blocks for a modify verdict), ``kind`` (the string
passed to ``dispatch``), ``mech`` (how it's installed: ``"gum"`` = frida-gum inline attach;
``"fullcgo"`` / ``"asmcgo"`` = cgocall trampoline via full ``runtime.cgocall`` vs the lighter
``runtime.asmcgocall``; ``"off"`` = NOT installed — kept as documentation of a hook that
stalls agy or collides with another), ``leave`` (intercepts the return value), ``note``.

The shim installs the union of every non-``"off"`` hook on each run (no stage selector).
``"off"`` hooks are the parking/return-value funcs and the os.Getenv duplicate.
"""

HOOKS = [
    {"id": "SMOKE_GETENV", "symbol": "os.Getenv", "mode": "async", "kind": "smoke",
     "mech": "gum", "leave": False, "note": "liveness smoke; fires often, no network"},
    {"id": "FILE_OPEN", "symbol": "os.OpenFile", "mode": "async", "kind": "file_open",
     "mech": "gum", "leave": False,
     "note": "OVERLAY (AGY_PROC_CONV_ID): enter-only probe reading OpenFile's path arg; "
             "C-filtered to conversations/·/brain/ paths → conversation_id event (the uuid "
             "is in the path). Only attaches when AGY_PROC_CONV_ID is set."},
    {"id": "TLS_WRITE", "symbol": "crypto/tls.(*Conn).Write", "mode": "async",
     "kind": "tls_write", "mech": "gum", "leave": False,
     "note": "egress c2s; the ONLY rewrite surface (AGY_PROC_TLS_WRITE_SYNC); "
             "carries the full HTTP/1.1 model request"},
    {"id": "H2_PIPE_WRITE", "symbol": "net/http/internal/http2.(*pipe).Write",
     "mode": "async", "kind": "resp", "mech": "fullcgo", "leave": False,
     "note": "de-framed HTTP/2 response chunk ([]byte entry arg); parks → cgocall trampoline"},
    {"id": "TLS_DECRYPT", "symbol": "crypto/tls.(*halfConn).decrypt", "mode": "async",
     "kind": "tls_read", "mech": "gum", "leave": True,
     "note": "ingress s2c (decrypted inbound records); carries the SSE response"},
    {"id": "TLS_READ", "symbol": "crypto/tls.(*Conn).Read", "mode": "async",
     "kind": "tls_read", "mech": "off", "leave": True,
     "note": "OFF: trampoline-hookable only via full cgocall (asmcgo stalls the turn 0/3 vs 2/2 "
             "baseline; full cgocall completes 4/6 — inverts the 'asmcgo for hot funcs' rule). "
             "Kept off anyway: no data (plaintext is the return value; response is on TLS_DECRYPT) "
             "and ~135 cgocalls/turn. Parks under gum."},
    {"id": "HTTP_RT", "symbol": "net/http.(*Transport).RoundTrip", "mode": "async",
     "kind": "http_rt", "mech": "asmcgo", "leave": False,
     "note": "RoundTrip marker (req ptr = rbx); parks → trampoline. Hot + about to syscall → asmcgo"},
    {"id": "SER_ROOT",
     "symbol": "google3/third_party/jetski/cli/model/model.(*RootModel).Serialize",
     "mode": "async", "kind": "serialize", "mech": "gum", "leave": True,
     "note": "app-layer R&D; empirically does not fire for the model request"},
    {"id": "MAR_PROMPT",
     "symbol": "google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON",
     "mode": "async", "kind": "marshal", "mech": "gum", "leave": True, "note": "app-layer R&D"},
    {"id": "PROTO_MARSHAL",
     "symbol": "google3/third_party/golang/gogo/protobuf/proto/proto.Marshal",
     "mode": "async", "kind": "proto_marshal", "mech": "gum", "leave": True,
     "note": "app-layer R&D; hot path (fires on every proto marshal ≥256B)"},
    {"id": "CGT_SEND_USER_MSG",
     "symbol": "google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage",
     "mode": "async", "kind": "send_user_msg", "mech": "fullcgo", "leave": False,
     "note": "cgocall-trampoline app-boundary hook (observe)"},
    {"id": "CGT_STREAM_SEND",
     "symbol": "google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send",
     "mode": "async", "kind": "stream_send", "mech": "asmcgo", "leave": False,
     "note": "app-boundary hook (observe); hot + syscall-at-entry-sensitive → asmcgo"},
    {"id": "CGT_GETENV", "symbol": "os.Getenv", "mode": "async", "kind": "cgt_getenv",
     "mech": "off", "leave": False,
     "note": "OFF: os.Getenv is already gum-hooked by SMOKE_GETENV; installing both would "
             "double-patch one entry (overlapping SMC → crash). Trampoline validator, redundant."},
    # model-text pipeline probe (diagnostic/RE only — the response data path stays on the
    # wire via http1sse; see README "App-boundary text probe"). Installed (park-safe) but
    # empirically inert on 1.0.16.
    {"id": "GET_DELTA_CCPA",
     "symbol": "…api_server_go_proto.(*GetChatMessageResponse).GetDeltaText",
     "mode": "async", "kind": "delta_ccpa", "mech": "gum", "leave": True,
     "note": "leaf getter, returns delta text (on_leave); inactive ccpa provider — doesn't fire on 1.0.16"},
    {"id": "GET_DELTA_CMPL",
     "symbol": "…codeium_common_go_proto.(*CompletionDelta).GetDeltaText",
     "mode": "async", "kind": "delta_completion", "mech": "gum", "leave": True,
     "note": "leaf getter, returns delta text (on_leave); inactive provider — doesn't fire"},
    # RESPONSE getters (return the assembled text as a plain string on_leave). Tried for
    # the return-value problem; DON'T fire on 1.0.16 (framework reads the field directly).
    {"id": "RESP_TEXT", "symbol": "…cortex_go_proto.(*CortexStepPlannerResponse).GetResponse",
     "mode": "async", "kind": "resp_text", "mech": "gum", "leave": True, "note": "response getter — doesn't fire (direct field access)"},
    {"id": "RESP_THINKING", "symbol": "…cortex_go_proto.(*CortexStepPlannerResponse).GetThinking",
     "mode": "async", "kind": "resp_thinking", "mech": "gum", "leave": True, "note": "thinking getter — doesn't fire"},
    {"id": "RESP_VIEW", "symbol": "…trajectory.(*PlannerResponseStepView).Response",
     "mode": "async", "kind": "resp_view", "mech": "gum", "leave": True, "note": "response view — doesn't fire"},
    {"id": "FH_FINALIZE",
     "symbol": "…generator.(*streamResponseHandler).finalizePlannerResponse",
     "mode": "async", "kind": "fh_finalize", "mech": "fullcgo", "leave": False,
     "note": "framework choke point (trampoline); fires, but output text is built during the call → not in entry args"},
    {"id": "FH_UPDATE",
     "symbol": "…generator.(*streamResponseHandler).updateWithStep",
     "mode": "async", "kind": "fh_update", "mech": "fullcgo", "leave": False,
     "note": "THE shallow response consumer: RSI→planner response, answer text at "
             "+0x8/+0x10 (one deref) → decoded to `app_response` in agy_cgo_hook"},
    {"id": "FH_PROCESS",
     "symbol": "…generator.(*streamResponseHandler).processStream",
     "mode": "async", "kind": "fh_process", "mech": "fullcgo", "leave": False,
     "note": "framework stream consumer (trampoline)"},
    {"id": "CORE_PLANSTEP",
     "symbol": "…core.createPlannerResponseStep",
     "mode": "async", "kind": "core_planstep", "mech": "fullcgo", "leave": False,
     "note": "builds assistant Step (trampoline); inlined/off-path — fired 0× in probe"},
    # consumer-entry hooks for the RESPONSE (return-value problem). OnStepsChanged FIRES
    # and its entry-reachable graph holds the full assembled answer (via AGY_PROC_CGT_ARGS);
    # the AppendStep/AddStep variants don't fire (different concrete type / --print path).
    {"id": "TRAJ_APPENDSTEP", "symbol": "…integration.(*ToolContextTrajectory).AppendStep",
     "mode": "async", "kind": "traj_appendstep", "mech": "fullcgo", "leave": False, "note": "doesn't fire in --print/interactive"},
    {"id": "TRAJ_ADDSTEP", "symbol": "…cortex/traj/traj.(*Trajectory).AddStep",
     "mode": "async", "kind": "traj_addstep", "mech": "fullcgo", "leave": False, "note": "doesn't fire"},
    {"id": "TRAJ_ONSTEPS", "symbol": "…agent_state_component.(*AgentState).OnStepsChanged",
     "mode": "async", "kind": "traj_onsteps", "mech": "fullcgo", "leave": False,
     "note": "FIRES; entry graph holds the full assembled response (deep offset; extraction fragile → superseded by fh_update)"},
    {"id": "TRAJ_APPENDSTEP_EXEC", "symbol": "…framework/executor/executor.(*ExecutionTrajectory).AppendStep",
     "mode": "async", "kind": "traj_appendstep_exec", "mech": "fullcgo", "leave": False,
     "note": "FIRES on the --print path; the commit point one frame above OnStepsChanged "
             "(chain endpoint + stack anchor). *Step text is 6 hops deep → decode lives on fh_update"},
    # CodeAssistClient RPC trace (trampoline; kind = RPC label). The app-semantic backend
    # boundary — request via AGY_PROC_CGT_ARGS, stack via AGY_PROC_STACK.
    # StreamGenerateContent is the model turn. See rpctrace.py.
    {"id": "RPC_STREAM_GEN", "symbol": "…codeassistclient.(*CodeAssistClient).StreamGenerateContent",
     "mode": "async", "kind": "rpc_stream_generate", "mech": "fullcgo", "leave": False,
     "note": "the model turn (streaming); request proto at entry"},
    {"id": "RPC_GEN", "symbol": "…(*CodeAssistClient).GenerateContent",
     "mode": "async", "kind": "rpc_generate", "mech": "fullcgo", "leave": False, "note": "non-streaming generate"},
    {"id": "RPC_LOAD_CA", "symbol": "…(*CodeAssistClient).FetchLoadCodeAssistResponse",
     "mode": "async", "kind": "rpc_load_code_assist", "mech": "fullcgo", "leave": False, "note": "startup"},
    {"id": "RPC_USERINFO", "symbol": "…(*CodeAssistClient).FetchUserInfo",
     "mode": "async", "kind": "rpc_fetch_userinfo", "mech": "fullcgo", "leave": False, "note": "startup"},
    {"id": "RPC_MODELS", "symbol": "…(*CodeAssistClient).FetchAvailableModels",
     "mode": "async", "kind": "rpc_fetch_models", "mech": "fullcgo", "leave": False, "note": "MendelStateCache pollLoop"},
    {"id": "RPC_EXPERIMENTS", "symbol": "…(*CodeAssistClient).ListExperiments",
     "mode": "async", "kind": "rpc_list_experiments", "mech": "fullcgo", "leave": False, "note": "MendelStateCache pollLoop"},
    {"id": "RPC_QUOTA", "symbol": "…(*CodeAssistClient).RetrieveUserQuotaSummary",
     "mode": "async", "kind": "rpc_quota", "mech": "fullcgo", "leave": False, "note": "store quotaRefreshLoop"},
    {"id": "RPC_REC_OFFERED", "symbol": "…(*CodeAssistClient).RecordConversationOffered",
     "mode": "async", "kind": "rpc_record_offered", "mech": "fullcgo", "leave": False, "note": "telemetry"},
    {"id": "RPC_REC_TRAJ", "symbol": "…(*CodeAssistClient).RecordTrajectorySegmentAnalytics",
     "mode": "async", "kind": "rpc_record_trajectory", "mech": "fullcgo", "leave": False,
     "note": "post-turn telemetry (AgentExecutor.recordTelemetryAfterExecution)"},
    {"id": "RPC_WRITE_ACLS", "symbol": "…(*CodeAssistClient).WriteTrajectoryACLs",
     "mode": "async", "kind": "rpc_write_acls", "mech": "fullcgo", "leave": False, "note": "trajectory ACLs"},
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
    "app_response": "assembled assistant answer decoded at updateWithStep (rsi+0x8) — the app-boundary RESPONSE",
}


def by_kind(kind):
    for h in HOOKS:
        if h["kind"] == kind:
            return h
    return None


def by_mech(mech):
    """Hooks by mechanism: ``"gum"``, ``"fullcgo"``, ``"asmcgo"``, or ``"off"``."""
    return [h for h in HOOKS if h["mech"] == mech]


def enabled_hooks():
    """The hooks actually installed on each run (the full working union)."""
    return [h for h in HOOKS if h["mech"] != "off"]


def sync_capable():
    """Kinds that can rewrite egress (today: only tls_write)."""
    return [h["kind"] for h in HOOKS if h["kind"] == "tls_write"]
