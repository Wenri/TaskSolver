/* procdef.h — the hook table as a plain constexpr array + a consteval index lookup.
 *
 * No X-macro: HOOKS is ordinary data, and each hook is addressed by a compile-time
 * `hk("ID")` lookup instead of a generated HK_* enum (a typo is a compile error — the
 * consteval `hk` can't reach its throw for a real id). Each row's Go-symbol vaddr/skip
 * are RESOLVED AT COMPILE TIME by agy_hook's consteval constructor (agy_sym/agy_skip
 * from symbols_gen.h), so the runtime install loop reads baked fields and never does a
 * symbol lookup. HOOKS[hk("X")] is X's row.
 *
 *   { "ID", "go.symbol.Name", MODE, "kind", MECH, LEAVE, RETMIN }
 *
 * ID     short unique tag, used by hk("ID") at the call sites (was the HK_* enum name).
 * MODE   AGY_ASYNC (log, non-blocking) or AGY_SYNC (block for a modify verdict).
 * kind   string tag passed to Python dispatch(kind, stream_id, data).
 * MECH   how the hook is installed (see install_hooks in antigravity.cpp):
 *          AGY_GUM     = frida-gum inline attach. RETIRED — no hook uses it. Its self-modifying-code
 *                        patching intermittently destabilizes agy's Go runtime: leave=1 gum trips the
 *                        GC unwinder (~40% turn failure), and even ENTRY gum on hot funcs fails the
 *                        full AgyProcess path under worker load (~15%). A 2026-07 bisect showed both
 *                        cgocall-trampoline variants below are 100% where gum is ~60-85%. Kept as an
 *                        enum only for the on_enter/on_leave register-read machinery.
 *          AGY_FULLCGO = cgocall trampoline (cgotrampoline.cpp) via full runtime.cgocall — for PARKING
 *                        funcs (entersyscall + P handoff, GC-safe). The default.
 *          AGY_ASMCGO  = cgocall trampoline via runtime.asmcgocall — the lighter g0-switch variant
 *                        (no syscall transition), for HOT NON-parking funcs (os.Getenv, tls_write):
 *                        same 100% reliability, less per-fire overhead. Falls back to full cgocall if
 *                        runtime.asmcgocall is unresolved. NOT for parking funcs (no P handoff → stall).
 *          AGY_OFF     = NOT installed. Kept here (row + on_enter/on_leave case) as
 *                        documentation of a hook that stalls agy or collides with another.
 *        The shim installs the union of every non-OFF hook on each run (no stage selector).
 *        FULLCGO and ASMCGO hooks share one trampoline region + synthetic moduledata; the
 *        pcsp matches the full-cgo geometry (only those slots are ever GC-unwound).
 * LEAVE  1 => needs on_leave (return-value interception). Costs a return-address rewrite that
 *        Go's stack unwinder trips on — and a 2026-07 bisect proved it: the gum leave=1 hooks
 *        intermittently corrupt agy's GC stack-unwind, killing ~40% of model turns (raw agy: 0%;
 *        entry-only gum + trampolines: 0%). So every gum leave=1 hook is now AGY_OFF. To read a
 *        value a leave hook used to intercept, hook an ENTRY-arg func via the trampoline instead
 *        (it reads args on the g0 stack and never rewrites a return) — see H2_PIPE_WRITE / the
 *        cgt_* app hooks. NEVER hook runtime-special funcs (runtime.main, goroutine entries) —
 *        Go validates their return PC.
 * RETMIN 0 for most hooks. >0 marks a "returns []byte/string" leaf getter whose on_leave path
 *        emits the RAX=ptr/RBX=len return value (stream_id 0), gated by len >= RETMIN (e.g. 256
 *        skips tiny protos on the hot proto.Marshal). Data-drives on_leave's generic return-bytes
 *        branch; 0 leaves a hook to the special-cased branches (TLS_READ/TLS_DECRYPT) or none.
 */
#ifndef AGY_PROCDEF_H
#define AGY_PROCDEF_H

#include "pybridge.h"      /* agy_mode_t (AGY_ASYNC / AGY_SYNC) */
#include "symbols_gen.h"   /* consteval agy_sym / agy_skip — resolve vaddr/skip at compile time */
#include <cstdint>
#include <string_view>

/* How each hook is installed. AGY_OFF must be 0 so a hook is only ever installed when explicitly
 * set. AGY_FULLCGO / AGY_ASMCGO are both cgocall-trampoline hooks (the Go→C gateway differs:
 * full runtime.cgocall vs the lighter runtime.asmcgocall); see the MECH doc above. */
enum agy_mech_t { AGY_OFF = 0, AGY_GUM, AGY_FULLCGO, AGY_ASMCGO };

/* One table row. `vaddr`/`skip` are NOT written per row — the consteval constructor resolves
 * them from `name` at compile time via agy_sym/agy_skip (0 if unresolved → install skips + logs). */
struct agy_hook {
    std::string_view id;      /* short unique id, for hk() compile-time lookup */
    const char      *name;    /* Go symbol → agy_sym */
    agy_mode_t       mode;
    const char      *kind;    /* → dispatch(kind,…) / agy_event_t.kind */
    agy_mech_t       mech;
    int              leave;
    uint32_t         retmin;
    uint64_t         vaddr;    /* = agy_sym(name), baked at compile time */
    uint32_t         skip;     /* = agy_skip(name), baked at compile time */

    consteval agy_hook(std::string_view id_, const char *name_, agy_mode_t mode_,
                       const char *kind_, agy_mech_t mech_, int leave_, uint32_t retmin_)
        : id(id_), name(name_), mode(mode_), kind(kind_), mech(mech_), leave(leave_),
          retmin(retmin_), vaddr(agy_sym(name_)), skip(agy_skip(name_)) {}
};

inline constexpr agy_hook HOOKS[] = {
/* Safe ordinary startup function; fires many times, no auth/network needed. Liveness smoke.
 * Hot (~116 fires/turn) + non-parking → AGY_ASMCGO (the lighter trampoline; was gum, retired).
 * CGT_GETENV below (same os.Getenv entry) stays AGY_OFF to avoid double-patching. */
{ "SMOKE_GETENV", "os.Getenv",                      AGY_ASYNC, "smoke",     AGY_ASMCGO,   0, 0 },

/* Clean end-of-capture marker. os.Exit(code) is the explicit clean-exit path — a crash/panic
 * won't fire it, which is correct (the capture then has no clean marker). Fires once; a normal Go
 * func (register ABI, spliceable prologue, live scheduler) → FULLCGO, unlike runtime.exit (tiny
 * teardown assembly). The "exit" branch in agy_cgo_hook reads code=rax and SYNC-emits it so the
 * worker writes {"kind":"exit","code":N} BEFORE agy's exit_group syscall. */
{ "EXIT",         "os.Exit",                        AGY_SYNC,  "exit",      AGY_FULLCGO,  0, 0 },

/* conversation-id capture (overlay, gated by AGY_PROC_CONV_ID — skipped unless that is set).
 * os.OpenFile: agy opens .../conversations/<uuid>.db and .../brain/<uuid>/.../transcript.jsonl,
 * so the uuid is in the path arg. AGY_FULLCGO — rare (gated) + does an openat syscall (wants the
 * P handoff), so NOT an asmcgo candidate (asmcgo is reserved for HOT non-parking funcs). NOTE:
 * the path filter still lived in the retired gum on_enter case; re-enabling conv-id capture needs
 * a "file_open" arg-read in agy_cgo_hook (a follow-on, like the tls_write read). */
{ "FILE_OPEN",    "os.OpenFile",                     AGY_ASYNC, "file_open", AGY_FULLCGO,   0, 0 },

/* egress capture — the full model REQUEST. Non-parking + hot (~55 fires/turn) → AGY_ASMCGO
 * (lighter trampoline; was gum, retired). agy_cgo_hook reads the request bytes as an entry arg
 * (c=rax, b.ptr=rbx, b.len=rcx) — the reliable replacement for the retired gum on_enter read. */
{ "TLS_WRITE",    "crypto/tls.(*Conn).Write",        AGY_ASYNC, "tls_write", AGY_ASMCGO,   0, 0 },
/* http2 pipe.Write gets each de-framed response body chunk as []byte (receiver=RAX,
 * p.ptr=RBX, p.len=RCX). It PARKS under reader/writer mutex/cond contention, so a gum
 * attach stalls agy — hooked via the park-safe cgocall trampoline instead (the chunk is
 * an ENTRY arg, safe-read off the g0 stack in agy_cgo_hook). A more-direct HTTP/2 body
 * capture; overlaps the TLS_DECRYPT → h2reassemble path. */
{ "H2_PIPE_WRITE","net/http/internal/http2.(*pipe).Write", AGY_ASYNC, "resp",   AGY_FULLCGO, 0, 0 },
/* AGY_OFF — the ingress RESPONSE (decrypted inbound record) is an on_leave []byte, but gum
 * leave=1 on this hot func trips the GC unwinder (~40% turn failure — see header LEAVE). Retired;
 * the model response is to be re-captured via an entry-arg trampoline hook. Until then genai_turn
 * carries the request (TLS_WRITE) but not the wire response; the answer comes from FH_UPDATE. */
{ "TLS_DECRYPT",  "crypto/tls.(*halfConn).decrypt",   AGY_ASYNC, "tls_read",  AGY_OFF,   1, 0 },
/* AGY_OFF — Conn.Read is trampoline-hookable ONLY via full cgocall, not asmcgo (tested):
 * asmcgo (no P handoff) stalled the model turn 0/3 while the baseline was 2/2; full cgocall
 * (entersyscall + P handoff) completed 4/6. The hot netpoll read path needs the P handed off
 * on each fire, so the usual "asmcgo for hot funcs" heuristic is INVERTED here. Left OFF
 * anyway: it yields NO data (the plaintext is the RETURN value the entry-only trampoline
 * can't read; the response is already captured at the non-parking TLS_DECRYPT above), while
 * full-cgo costs ~135 cgocalls/turn + as many _Gsyscall GC-scan windows on the read path —
 * all for a bare fire marker. */
{ "TLS_READ",     "crypto/tls.(*Conn).Read",         AGY_ASYNC, "tls_read",  AGY_OFF,    1, 0 },
/* RoundTrip(t=RAX, req=RBX) PARKS on the round-trip → cgocall trampoline, AGY_FULLCGO (parking →
 * needs the P handoff; asmcgo would risk a stall). Emits an http_rt marker keyed by the request
 * ptr; the response is a RETURN the trampoline can't read and the request is already captured via
 * TLS_WRITE, so this is a fire/timing marker more than new data. */
{ "HTTP_RT",      "net/http.(*Transport).RoundTrip", AGY_ASYNC, "http_rt",   AGY_FULLCGO,  0, 0 },

/* AGY_OFF — app-layer capture R&D (CPU-only funcs returning []byte). Retired: gum leave=1 is
 * GC-unwind-unsafe (see header LEAVE), and these never fired for the model request anyway. */
{ "SER_ROOT",     "google3/third_party/jetski/cli/model/model.(*RootModel).Serialize",    AGY_ASYNC, "serialize", AGY_OFF,   1, 1 },
{ "MAR_PROMPT",   "google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON", AGY_ASYNC, "marshal",   AGY_OFF,   1, 1 },
{ "PROTO_MARSHAL","google3/third_party/golang/gogo/protobuf/proto/proto.Marshal",            AGY_ASYNC, "proto_marshal", AGY_OFF,   1, 256 },

/* cgocall-TRAMPOLINE app-boundary hooks (Approach A — the robust general mechanism).
 * The parking targets (SendUserMessage/callbackStreamer.Send): instead of a gum
 * attach (whose return-tracking breaks on park/reschedule) we redirect them through
 * a generated trampoline (cgotrampoline.cpp) + a synthetic moduledata so GC stack-unwind is safe.
 * MODE/leave are advisory here — the trampoline reads args on the g0 stack and never
 * intercepts the return. Both PARK → AGY_FULLCGO (the P handoff; asmcgo would risk a stall). */
{ "CGT_SEND_USER_MSG", "google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage", AGY_ASYNC, "send_user_msg", AGY_FULLCGO, 0, 0 },
{ "CGT_STREAM_SEND",   "google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send",          AGY_ASYNC, "stream_send",   AGY_FULLCGO,  0, 0 },

/* AGY_OFF — cgocall-trampoline validation probe on the BENIGN os.Getenv. Disabled because
 * os.Getenv is ALSO hooked by SMOKE_GETENV (gum) above; installing both would patch one
 * function entry with a gum inline hook AND a trampoline redirect (overlapping SMC → crash).
 * The trampoline mechanism is exercised by the real trampoline hooks; this validator is redundant. */
{ "CGT_GETENV", "os.Getenv", AGY_ASYNC, "cgt_getenv", AGY_OFF, 0, 0 },

/* AGY_OFF — model-TEXT leaf getters (gemini_coder pipeline): frameless nosplit getters that
 * RETURN the streamed assistant text (on_leave RAX=ptr,RBX=len). Retired: gum leave=1 is
 * GC-unwind-unsafe (see header LEAVE), and being frameless they aren't trampoline-hookable
 * either (no prologue to relocate). The answer is captured at FH_UPDATE (app_response) instead. */
{ "GET_DELTA_CCPA", "google3/third_party/jetski/api_server_pb/api_server_go_proto.(*GetChatMessageResponse).GetDeltaText", AGY_ASYNC, "delta_ccpa", AGY_OFF,   1, 1 },
{ "GET_DELTA_CMPL", "google3/third_party/jetski/codeium_common_pb/codeium_common_go_proto.(*CompletionDelta).GetDeltaText", AGY_ASYNC, "delta_completion", AGY_OFF,   1, 1 },
/* AGY_OFF — RESPONSE getters (assembled assistant text as a Go string, on_leave RAX=ptr,RBX=len).
 * Retired for the same reason (gum leave=1 is GC-unwind-unsafe; see header LEAVE). */
{ "RESP_TEXT",     "google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetResponse", AGY_ASYNC, "resp_text",     AGY_OFF,   1, 1 },
{ "RESP_THINKING", "google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetThinking", AGY_ASYNC, "resp_thinking", AGY_OFF,   1, 1 },
{ "RESP_VIEW",     "google3/third_party/jetski/cortex/trajectory/trajectory.(*PlannerResponseStepView).Response",   AGY_ASYNC, "resp_view",     AGY_OFF,   1, 1 },

/* model-TEXT framework choke points (provider-agnostic). On the parking
 * stream-consumer path → cgocall trampoline; AGY_PROC_CGT_ARGS walks their args to
 * locate the accumulated/per-delta assistant text. finalize = full per-turn text. */
{ "FH_FINALIZE", "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).finalizePlannerResponse", AGY_ASYNC, "fh_finalize", AGY_FULLCGO, 0, 0 },
/* THE clean response consumer (Plan 7 probe winner): updateWithStep's RSI arg points
 * at the planner response; the assistant text is a Go string at +0x8/+0x10 — ONE
 * deref, the stable cortex proto layout. agy_cgo_hook decodes it directly to
 * `app_response` (far shallower than AppendStep/OnStepsChanged's 6-deep graphs). */
{ "FH_UPDATE",   "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).updateWithStep",          AGY_ASYNC, "fh_update",   AGY_FULLCGO, 0, 0 },
{ "FH_PROCESS",  "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).processStream",           AGY_ASYNC, "fh_process",  AGY_FULLCGO, 0, 0 },
{ "CORE_PLANSTEP", "google3/third_party/gemini_coder/framework/core/core.createPlannerResponseStep",                                AGY_ASYNC, "core_planstep", AGY_FULLCGO, 0, 0 },
/* consumer-entry hook for the RESPONSE (solves the return-value problem without
 * capturing a return): AppendStep/SetStep take the completed *Step — with the
 * assembled assistant text (GetPlannerResponse→GetResponse) — as an ENTRY arg.
 * Trampoline (they park committing to the trajectory); walk with AGY_PROC_CGT_ARGS. */
{ "TRAJ_APPENDSTEP", "google3/third_party/gemini_coder/framework/core/integration/integration.(*ToolContextTrajectory).AppendStep", AGY_ASYNC, "traj_appendstep", AGY_FULLCGO, 0, 0 },
{ "TRAJ_ADDSTEP",    "google3/third_party/jetski/cortex/traj/traj.(*Trajectory).AddStep",                                             AGY_ASYNC, "traj_addstep",    AGY_FULLCGO, 0, 0 },
{ "TRAJ_ONSTEPS",    "google3/third_party/jetski/cortex/agent_state_component/agent_state_component.(*AgentState).OnStepsChanged",     AGY_ASYNC, "traj_onsteps",    AGY_FULLCGO, 0, 0 },
/* Plan 7 — the commit point one frame above OnStepsChanged (runExecution → AppendStep
 * → observers). Fires on the live --print path (unlike ToolContextTrajectory.AppendStep
 * above). Kept as a documented chain endpoint + stack anchor; the live probe found its
 * *Step text is 6 struct-hops deep (as fragile as OnStepsChanged), so the clean
 * `app_response` decode lives on FH_UPDATE, not here. */
{ "TRAJ_APPENDSTEP_EXEC", "google3/third_party/gemini_coder/framework/executor/executor.(*ExecutionTrajectory).AppendStep",           AGY_ASYNC, "traj_appendstep_exec", AGY_FULLCGO, 0, 0 },

/* CodeAssistClient RPC trace. (*CodeAssistClient).* is agy's single client
 * to the CloudCode backend — each method is one named RPC with typed proto args. They
 * park on the HTTP round-trip → cgocall trampoline. The kind is the RPC label (→ a
 * time-ordered app-level trace); AGY_PROC_STACK adds the call stack, AGY_PROC_CGT_ARGS
 * walks the request proto at entry. StreamGenerateContent is the model turn itself. */
#define CAC "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient)."
{ "RPC_STREAM_GEN",   CAC "StreamGenerateContent",            AGY_ASYNC, "rpc_stream_generate",   AGY_FULLCGO, 0, 0 },
{ "RPC_GEN",          CAC "GenerateContent",                  AGY_ASYNC, "rpc_generate",          AGY_FULLCGO, 0, 0 },
{ "RPC_LOAD_CA",      CAC "FetchLoadCodeAssistResponse",      AGY_ASYNC, "rpc_load_code_assist",  AGY_FULLCGO, 0, 0 },
{ "RPC_USERINFO",     CAC "FetchUserInfo",                    AGY_ASYNC, "rpc_fetch_userinfo",    AGY_FULLCGO, 0, 0 },
{ "RPC_MODELS",       CAC "FetchAvailableModels",             AGY_ASYNC, "rpc_fetch_models",      AGY_FULLCGO, 0, 0 },
{ "RPC_EXPERIMENTS",  CAC "ListExperiments",                  AGY_ASYNC, "rpc_list_experiments",  AGY_FULLCGO, 0, 0 },
{ "RPC_QUOTA",        CAC "RetrieveUserQuotaSummary",         AGY_ASYNC, "rpc_quota",             AGY_FULLCGO, 0, 0 },
{ "RPC_REC_OFFERED",  CAC "RecordConversationOffered",        AGY_ASYNC, "rpc_record_offered",    AGY_FULLCGO, 0, 0 },
{ "RPC_REC_TRAJ",     CAC "RecordTrajectorySegmentAnalytics", AGY_ASYNC, "rpc_record_trajectory", AGY_FULLCGO, 0, 0 },
{ "RPC_WRITE_ACLS",   CAC "WriteTrajectoryACLs",              AGY_ASYNC, "rpc_write_acls",        AGY_FULLCGO, 0, 0 },
#undef CAC
};

inline constexpr int HK_COUNT = (int)(sizeof(HOOKS) / sizeof(HOOKS[0]));

/* Compile-time hook index by id: HOOKS[hk("TLS_READ")] etc. A typo is a COMPILE error —
 * the throw is unreachable for a real id, but reaching it aborts constant evaluation. */
consteval int hk(std::string_view id) {
    for (int i = 0; i < HK_COUNT; i++)
        if (HOOKS[i].id == id) return i;
    throw "unknown hook id";
}

#endif /* AGY_PROCDEF_H */
