/* procdef.h — declarative hook table (X-macro).
 *
 *   HOOK(ID, "go.symbol.Name", MODE, "kind", MECH, LEAVE)
 *
 * ID     enum tag used in the on_enter/on_leave switch (register semantics are
 *        coded per-ID in antigravity.c — different funcs read different registers).
 * MODE   AGY_ASYNC (log, non-blocking) or AGY_SYNC (block for a modify verdict).
 * kind   string tag passed to Python dispatch(kind, stream_id, data).
 * MECH   how the hook is installed (see install_hooks in antigravity.c):
 *          AGY_GUM     = frida-gum inline attach (non-parking CPU funcs; supports leave=1).
 *          AGY_FULLCGO = cgocall trampoline (cgotrampoline.c) via full runtime.cgocall — the robust
 *                        default for parking funcs (entersyscall + P handoff, GC-safe).
 *          AGY_ASMCGO  = cgocall trampoline via runtime.asmcgocall — the lighter g0-switch
 *                        variant (no syscall transition), for hot / syscall-at-entry-sensitive
 *                        funcs. Falls back to full cgocall if runtime.asmcgocall is unresolved.
 *          AGY_OFF     = NOT installed. Kept here (line + on_enter/on_leave case) as
 *                        documentation of a hook that stalls agy or collides with another.
 *        The shim installs the union of every non-OFF hook on each run (no stage selector).
 *        FULLCGO and ASMCGO hooks share one trampoline region + synthetic moduledata; the
 *        pcsp matches the full-cgo geometry (only those slots are ever GC-unwound).
 * LEAVE  1 => needs on_leave (return-value interception). Costs a return-address
 *        rewrite that Go's stack unwinder can trip on, so keep it to hooks that
 *        truly need the return value. NEVER hook runtime-special funcs
 *        (runtime.main, goroutine entries) — Go validates their return PC.
 */

/* Safe ordinary startup function; fires many times, no auth/network needed. Liveness smoke.
 * (os.Getenv is ALSO a trampoline target below (CGT_GETENV) — that one is AGY_OFF to avoid
 * double-patching the same entry; we keep the lighter gum probe here.) */
HOOK(SMOKE_GETENV, "os.Getenv",                      AGY_ASYNC, "smoke",     AGY_GUM,   0)

/* conversation-id capture (overlay, gated by AGY_PROC_CONV_ID — skipped unless that is set).
 * os.OpenFile(name string, ...) reads name.ptr=RAX/len=RBX (same shape as
 * os.Getenv); agy opens .../conversations/<uuid>.db and .../brain/<uuid>/.../transcript.jsonl,
 * so the uuid is in the path. Enter-only probe → park-safe across the openat syscall; the
 * on_enter case filters to conversation paths in C so Python only sees the match. */
HOOK(FILE_OPEN,    "os.OpenFile",                     AGY_ASYNC, "file_open", AGY_GUM,   0)

/* egress capture. tls_write is safe once we attach PAST the prologue
 * (per-hook skip from symbols.json) — it doesn't park, so no morestack re-entry
 * and no park-while-hooked hazard. Captures the full model REQUEST. */
HOOK(TLS_WRITE,    "crypto/tls.(*Conn).Write",        AGY_ASYNC, "tls_write", AGY_GUM,   0)
/* http2 pipe.Write gets each de-framed response body chunk as []byte (receiver=RAX,
 * p.ptr=RBX, p.len=RCX). It PARKS under reader/writer mutex/cond contention, so a gum
 * attach stalls agy — hooked via the park-safe cgocall trampoline instead (the chunk is
 * an ENTRY arg, safe-read off the g0 stack in agy_cgo_hook). A more-direct HTTP/2 body
 * capture; overlaps the TLS_DECRYPT → h2reassemble path. */
HOOK(H2_PIPE_WRITE,"net/http/internal/http2.(*pipe).Write", AGY_ASYNC, "resp",   AGY_FULLCGO, 0)
/* ingress RESPONSE via TLS decrypt (CPU-only, post-read → doesn't park). on_leave
 * []byte = decrypted inbound record (HTTP/2 frames of the response). */
HOOK(TLS_DECRYPT,  "crypto/tls.(*halfConn).decrypt",   AGY_ASYNC, "tls_read",  AGY_GUM,   1)
/* AGY_OFF — Conn.Read is trampoline-hookable ONLY via full cgocall, not asmcgo (tested):
 * asmcgo (no P handoff) stalled the model turn 0/3 while the baseline was 2/2; full cgocall
 * (entersyscall + P handoff) completed 4/6. The hot netpoll read path needs the P handed off
 * on each fire, so the usual "asmcgo for hot funcs" heuristic is INVERTED here. Left OFF
 * anyway: it yields NO data (the plaintext is the RETURN value the entry-only trampoline
 * can't read; the response is already captured at the non-parking TLS_DECRYPT above), while
 * full-cgo costs ~135 cgocalls/turn + as many _Gsyscall GC-scan windows on the read path —
 * all for a bare fire marker. */
HOOK(TLS_READ,     "crypto/tls.(*Conn).Read",         AGY_ASYNC, "tls_read",  AGY_OFF,    1)
/* RoundTrip(t=RAX, req=RBX) PARKS on the round-trip → cgocall trampoline. Hot (fires per
 * HTTP request) and about to do its own syscalls, so it uses the lighter AGY_ASMCGO variant
 * (no entersyscall-at-entry). Emits an http_rt marker keyed by the request ptr; the response
 * is a RETURN the trampoline can't read and the request is already captured via TLS_WRITE,
 * so this is a fire/timing marker more than new data. */
HOOK(HTTP_RT,      "net/http.(*Transport).RoundTrip", AGY_ASYNC, "http_rt",   AGY_ASMCGO,  0)

/* app-layer capture R&D — CPU-only funcs returning []byte (readable). Do not fire for
 * the model request in practice, but park-safe leaves, so installed as part of the union. */
HOOK(SER_ROOT,     "google3/third_party/jetski/cli/model/model.(*RootModel).Serialize",    AGY_ASYNC, "serialize", AGY_GUM,   1)
HOOK(MAR_PROMPT,   "google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON", AGY_ASYNC, "marshal",   AGY_GUM,   1)
HOOK(PROTO_MARSHAL,"google3/third_party/golang/gogo/protobuf/proto/proto.Marshal",            AGY_ASYNC, "proto_marshal", AGY_GUM,   1)

/* cgocall-TRAMPOLINE app-boundary hooks (Approach A — the robust general mechanism).
 * The parking targets (SendUserMessage/callbackStreamer.Send): instead of a gum
 * attach (whose return-tracking breaks on park/reschedule) we redirect them through
 * a generated trampoline (cgotrampoline.c) + a synthetic moduledata so GC stack-unwind is safe.
 * MODE/leave are advisory here — the trampoline reads args on the g0 stack and never
 * intercepts the return. SendUserMessage fires once/turn → full cgocall; Send is hot and
 * syscall-at-entry-sensitive → the lighter AGY_ASMCGO. */
HOOK(CGT_SEND_USER_MSG, "google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage", AGY_ASYNC, "send_user_msg", AGY_FULLCGO, 0)
HOOK(CGT_STREAM_SEND,   "google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send",          AGY_ASYNC, "stream_send",   AGY_ASMCGO,  0)

/* AGY_OFF — cgocall-trampoline validation probe on the BENIGN os.Getenv. Disabled because
 * os.Getenv is ALSO hooked by SMOKE_GETENV (gum) above; installing both would patch one
 * function entry with a gum inline hook AND a trampoline redirect (overlapping SMC → crash).
 * The trampoline mechanism is exercised by the real trampoline hooks; this validator is redundant. */
HOOK(CGT_GETENV, "os.Getenv", AGY_ASYNC, "cgt_getenv", AGY_OFF, 0)

/* model-TEXT leaf getters (gemini_coder pipeline). These frameless nosplit
 * getters RETURN the streamed assistant text as a Go string (on_leave RAX=ptr,RBX=len)
 * — the cleanest signal, zero struct-offset fragility. CPU-only leaves → gum-attach
 * on_leave is re-entry-safe. Two provider variants; whichever fires at runtime wins.
 * (build_symbols NOSPLIT_TARGETS resolves them skip=0, exempt from the prologue assert.) */
HOOK(GET_DELTA_CCPA, "google3/third_party/jetski/api_server_pb/api_server_go_proto.(*GetChatMessageResponse).GetDeltaText", AGY_ASYNC, "delta_ccpa", AGY_GUM,   1)
HOOK(GET_DELTA_CMPL, "google3/third_party/jetski/codeium_common_pb/codeium_common_go_proto.(*CompletionDelta).GetDeltaText", AGY_ASYNC, "delta_completion", AGY_GUM,   1)
/* RESPONSE getters: return the assembled assistant text as a plain Go string (on_leave
 * RAX=ptr,RBX=len) — the clean, zero-struct-offset answer to the return-value problem
 * IF they fire (proto getters can be bypassed by direct field access; probe first). */
HOOK(RESP_TEXT,     "google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetResponse", AGY_ASYNC, "resp_text",     AGY_GUM,   1)
HOOK(RESP_THINKING, "google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetThinking", AGY_ASYNC, "resp_thinking", AGY_GUM,   1)
HOOK(RESP_VIEW,     "google3/third_party/jetski/cortex/trajectory/trajectory.(*PlannerResponseStepView).Response",   AGY_ASYNC, "resp_view",     AGY_GUM,   1)

/* model-TEXT framework choke points (provider-agnostic). On the parking
 * stream-consumer path → cgocall trampoline; AGY_PROC_CGT_ARGS walks their args to
 * locate the accumulated/per-delta assistant text. finalize = full per-turn text. */
HOOK(FH_FINALIZE, "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).finalizePlannerResponse", AGY_ASYNC, "fh_finalize", AGY_FULLCGO, 0)
/* THE clean response consumer (Plan 7 probe winner): updateWithStep's RSI arg points
 * at the planner response; the assistant text is a Go string at +0x8/+0x10 — ONE
 * deref, the stable cortex proto layout. agy_cgo_hook decodes it directly to
 * `app_response` (far shallower than AppendStep/OnStepsChanged's 6-deep graphs). */
HOOK(FH_UPDATE,   "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).updateWithStep",          AGY_ASYNC, "fh_update",   AGY_FULLCGO, 0)
HOOK(FH_PROCESS,  "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).processStream",           AGY_ASYNC, "fh_process",  AGY_FULLCGO, 0)
HOOK(CORE_PLANSTEP, "google3/third_party/gemini_coder/framework/core/core.createPlannerResponseStep",                                AGY_ASYNC, "core_planstep", AGY_FULLCGO, 0)
/* consumer-entry hook for the RESPONSE (solves the return-value problem without
 * capturing a return): AppendStep/SetStep take the completed *Step — with the
 * assembled assistant text (GetPlannerResponse→GetResponse) — as an ENTRY arg.
 * Trampoline (they park committing to the trajectory); walk with AGY_PROC_CGT_ARGS. */
HOOK(TRAJ_APPENDSTEP, "google3/third_party/gemini_coder/framework/core/integration/integration.(*ToolContextTrajectory).AppendStep", AGY_ASYNC, "traj_appendstep", AGY_FULLCGO, 0)
HOOK(TRAJ_ADDSTEP,    "google3/third_party/jetski/cortex/traj/traj.(*Trajectory).AddStep",                                             AGY_ASYNC, "traj_addstep",    AGY_FULLCGO, 0)
HOOK(TRAJ_ONSTEPS,    "google3/third_party/jetski/cortex/agent_state_component/agent_state_component.(*AgentState).OnStepsChanged",     AGY_ASYNC, "traj_onsteps",    AGY_FULLCGO, 0)
/* Plan 7 — the commit point one frame above OnStepsChanged (runExecution → AppendStep
 * → observers). Fires on the live --print path (unlike ToolContextTrajectory.AppendStep
 * above). Kept as a documented chain endpoint + stack anchor; the live probe found its
 * *Step text is 6 struct-hops deep (as fragile as OnStepsChanged), so the clean
 * `app_response` decode lives on FH_UPDATE, not here. */
HOOK(TRAJ_APPENDSTEP_EXEC, "google3/third_party/gemini_coder/framework/executor/executor.(*ExecutionTrajectory).AppendStep",           AGY_ASYNC, "traj_appendstep_exec", AGY_FULLCGO, 0)

/* CodeAssistClient RPC trace. (*CodeAssistClient).* is agy's single client
 * to the CloudCode backend — each method is one named RPC with typed proto args. They
 * park on the HTTP round-trip → cgocall trampoline. The kind is the RPC label (→ a
 * time-ordered app-level trace); AGY_PROC_STACK adds the call stack, AGY_PROC_CGT_ARGS
 * walks the request proto at entry. StreamGenerateContent is the model turn itself. */
#define CAC "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient)."
HOOK(RPC_STREAM_GEN,   CAC "StreamGenerateContent",            AGY_ASYNC, "rpc_stream_generate",   AGY_FULLCGO, 0)
HOOK(RPC_GEN,          CAC "GenerateContent",                  AGY_ASYNC, "rpc_generate",          AGY_FULLCGO, 0)
HOOK(RPC_LOAD_CA,      CAC "FetchLoadCodeAssistResponse",      AGY_ASYNC, "rpc_load_code_assist",  AGY_FULLCGO, 0)
HOOK(RPC_USERINFO,     CAC "FetchUserInfo",                    AGY_ASYNC, "rpc_fetch_userinfo",    AGY_FULLCGO, 0)
HOOK(RPC_MODELS,       CAC "FetchAvailableModels",             AGY_ASYNC, "rpc_fetch_models",      AGY_FULLCGO, 0)
HOOK(RPC_EXPERIMENTS,  CAC "ListExperiments",                  AGY_ASYNC, "rpc_list_experiments",  AGY_FULLCGO, 0)
HOOK(RPC_QUOTA,        CAC "RetrieveUserQuotaSummary",         AGY_ASYNC, "rpc_quota",             AGY_FULLCGO, 0)
HOOK(RPC_REC_OFFERED,  CAC "RecordConversationOffered",        AGY_ASYNC, "rpc_record_offered",    AGY_FULLCGO, 0)
HOOK(RPC_REC_TRAJ,     CAC "RecordTrajectorySegmentAnalytics", AGY_ASYNC, "rpc_record_trajectory", AGY_FULLCGO, 0)
HOOK(RPC_WRITE_ACLS,   CAC "WriteTrajectoryACLs",              AGY_ASYNC, "rpc_write_acls",        AGY_FULLCGO, 0)
#undef CAC
