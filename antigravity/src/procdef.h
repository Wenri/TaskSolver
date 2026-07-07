/* procdef.h — the hook table as a plain constexpr array + a consteval index lookup.
 *
 * No X-macro: HOOKS is ordinary data, and each hook is addressed by a compile-time
 * `hk("ID")` lookup instead of a generated HK_* enum (a typo is a compile error — the
 * consteval `hk` can't reach its throw for a real id). Each row's Go-symbol vaddr/skip
 * are RESOLVED AT COMPILE TIME by agy_hook's consteval constructor (agy_sym/agy_skip
 * from symbols_gen.h), so the runtime install loop reads baked fields and never does a
 * symbol lookup. HOOKS[hk("X")] is X's row.
 *
 *   { "ID", "go.symbol.Name", MODE, "kind", MECH, RETCAP }
 *
 * ID     short unique tag, used by hk("ID") at the call sites (was the HK_* enum name).
 * MODE   AGY_ASYNC (log, non-blocking) or AGY_SYNC (block for a modify verdict).
 * kind   string tag passed to Python dispatch(kind, stream_id, data).
 * MECH   how the hook is installed (see install_hooks in antigravity.cpp):
 *          AGY_GUM     = frida-gum inline attach. RETIRED — no hook uses it. Its self-modifying-code
 *                        patching intermittently destabilizes agy's Go runtime: gum RETURN hooks trip
 *                        the GC unwinder (see RETCAP), and even ENTRY gum on hot funcs fails the full
 *                        AgyProcess path under worker load (~15%). All-trampoline is 100% (80/80) where
 *                        gum is worse. Kept as an enum only for the on_enter/on_leave register-read
 *                        machinery.
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
 * RETCAP how on_leave captures this hook's RETURN value. Also selects the gum listener: any nonzero
 *        RETCAP means the return is intercepted, which costs a return-address rewrite Go's stack
 *        unwinder trips on — while the return points at gum's trampoline, a GC unwind of that
 *        goroutine hits an unknown PC → throw("unknown pc")/crash (empty turn, exit 2). A 2026-07-05
 *        per-hook bisect (each RETCAP≠0 hook flipped to AGY_GUM alone, N turns via the full worker
 *        path) pinned the rule: crash probability scales with FIRES/TURN × P(GC-unwind in the window)
 *        — silent hooks that fire 0× on this agy build (SER_ROOT, MAR_PROMPT, PROTO_MARSHAL,
 *        GET_DELTA_*, RESP_TEXT/THINKING/VIEW — dead 1.0.16 paths; answer flows via gemini_coder →
 *        FH_UPDATE)
 *        = 0 crashes but 0 data; hot non-parking TLS_DECRYPT (per TLS record) = 74/80 ≈ 92%; parking
 *        TLS_READ (netpoll, resumes on another M) = 0/15 catastrophic (all-trampoline baseline: 80/80
 *        = 100%). So NO RETCAP≠0 hook is both safe AND useful — every one is AGY_OFF, and new captures
 *        read an ENTRY arg via the trampoline instead (see H2_PIPE_WRITE / the cgt_* app hooks, e.g.
 *        RESP_CHUNK replaced the TLS_DECRYPT response). NEVER hook runtime-special funcs (runtime.main,
 *        goroutine entries) — Go validates their return PC. Values:
 *           0   no on_leave — don't intercept the return.
 *          <0  intercept, but handled specially by ID in on_leave (TLS_READ/TLS_DECRYPT, which also
 *              need on_enter-saved state — a buffer ptr / conn). -1 is the marker; the value is unused.
 *          >0  a "returns []byte/string" leaf getter — on_leave emits the RAX=ptr/RBX=len return
 *              (stream_id 0) when len >= RETCAP (256 skips tiny protos on the hot proto.Marshal;
 *              1 = emit any non-empty). This is the only value on_leave data-drives.
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

/* One table row. `vaddr`/`skip` are NOT written per row — the consteval constructor resolves them
 * from `name` at compile time via agy_sym/agy_skip (0 if unresolved → install skips + logs). `retcap`
 * is the return-capture policy (0 none / <0 special / >0 min bytes) — see the RETCAP doc above; it
 * also decides the gum listener (retcap!=0 → the enter+leave listener). */
struct agy_hook {
    std::string_view id;      /* short unique id, for hk() compile-time lookup */
    const char      *name;    /* Go symbol → agy_sym */
    agy_mode_t       mode;
    const char      *kind;    /* → dispatch(kind,…) / agy_event_t.kind */
    agy_mech_t       mech;
    int32_t          retcap;  /* return-capture policy: 0 none, <0 special-cased, >0 min bytes to emit */
    uint64_t         vaddr;    /* = agy_sym(name), baked at compile time */
    uint32_t         skip;     /* = agy_skip(name), baked at compile time */

    consteval agy_hook(std::string_view id_, const char *name_, agy_mode_t mode_,
                       const char *kind_, agy_mech_t mech_, int32_t retcap_)
        : id(id_), name(name_), mode(mode_), kind(kind_), mech(mech_),
          retcap(retcap_), vaddr(agy_sym(name_)), skip(agy_skip(name_)) {}
};

inline constexpr agy_hook HOOKS[] = {
/* Safe ordinary startup function; fires many times, no auth/network needed. Liveness smoke.
 * Hot (~116 fires/turn) + non-parking → AGY_ASMCGO (the lighter trampoline; was gum, retired).
 * CGT_GETENV below (same os.Getenv entry) stays AGY_OFF to avoid double-patching. */
{ "SMOKE_GETENV", "os.Getenv",                      AGY_ASYNC, "smoke",     AGY_ASMCGO,  0 },

/* Clean end-of-capture marker. os.Exit(code) is the explicit clean-exit path — a crash/panic
 * won't fire it, which is correct (the capture then has no clean marker). Fires once; a normal Go
 * func (register ABI, spliceable prologue, live scheduler) → FULLCGO, unlike runtime.exit (tiny
 * teardown assembly). The "exit" branch in agy_cgo_hook reads code=rax and SYNC-emits it so the
 * worker writes {"kind":"exit","code":N} BEFORE agy's exit_group syscall. */
{ "EXIT",         "os.Exit",                        AGY_SYNC,  "exit",      AGY_FULLCGO, 0 },

/* conversation-id capture (overlay, gated by AGY_PROC_CONV_ID — skipped unless that is set).
 * os.OpenFile: agy opens .../conversations/<uuid>.db and .../brain/<uuid>/.../transcript.jsonl,
 * so the uuid is in the path arg. AGY_FULLCGO — rare (gated) + does an openat syscall (wants the
 * P handoff), so NOT an asmcgo candidate (asmcgo is reserved for HOT non-parking funcs). NOTE:
 * the path filter still lived in the retired gum on_enter case; re-enabling conv-id capture needs
 * a "file_open" arg-read in agy_cgo_hook (a follow-on, like the tls_write read). */
{ "FILE_OPEN",    "os.OpenFile",                     AGY_ASYNC, "file_open", AGY_FULLCGO, 0 },

/* /proc/self/exe correction via the trampoline FILTER mode. Under `ld.so --preload`, the kernel's
 * /proc/self/exe points at the loader, not agy — so os.Executable()/os-init readlink (and agy's
 * self-re-exec) misresolve. We hook os.readlink (the inner, loop-bearing impl — NOT os.Readlink,
 * which Go inlines so its symbol never fires; verified empirically). Signature is the same,
 * os.readlink(name string)(string,error): name.ptr=rax,name.len=rbx; returns string.ptr=rax,
 * string.len=rbx,err.tab=rcx,err.data=rdi. The agy_cgo_hook branch filters name=="/proc/self/exe":
 * for it, it writes the real agy path (from AGY_PROC_REAL_EXE) into the return regs + sets
 * block.action → RETURN (skips the body); every other readlink PASSes. FULLCGO — the PASS branch runs
 * a readlinkat syscall that can park (os.OpenFile precedent). Fires once/turn at os-init, so it's a
 * deterministic, login-independent liveness signal. */
{ "READLINK_FILTER", "os.readlink",                  AGY_ASYNC, "readlink_filter", AGY_FULLCGO, 0 },

/* egress capture — the full model REQUEST. Non-parking + hot (~55 fires/turn) → AGY_ASMCGO
 * (lighter trampoline; was gum, retired). agy_cgo_hook reads the request bytes as an entry arg
 * (c=rax, b.ptr=rbx, b.len=rcx) — the reliable replacement for the retired gum on_enter read. */
{ "TLS_WRITE",    "crypto/tls.(*Conn).Write",        AGY_ASYNC, "tls_write", AGY_ASMCGO,  0 },
/* http2 pipe.Write gets each de-framed response body chunk as []byte (receiver=RAX,
 * p.ptr=RBX, p.len=RCX). It PARKS under reader/writer mutex/cond contention, so a gum
 * attach stalls agy — hooked via the park-safe cgocall trampoline instead (the chunk is
 * an ENTRY arg, safe-read off the g0 stack in agy_cgo_hook). A more-direct HTTP/2 body
 * capture; overlaps the TLS_DECRYPT → h2reassemble path. */
{ "H2_PIPE_WRITE","net/http/internal/http2.(*pipe).Write", AGY_ASYNC, "resp",   AGY_FULLCGO, 0 },
/* AGY_OFF — the ingress RESPONSE (decrypted inbound record) is an on_leave []byte. It FIRES and
 * carries real data, but per TLS record (hot) → a gum return hook accumulates GC-unwind windows:
 * measured 74/80 ≈ 92% (~8-12% hard crash) in the 2026-07-05 bisect — intermittently unsafe (see
 * header RETCAP). The wire RESPONSE is instead captured via the RESP_CHUNK entry-arg trampoline hook
 * (toStreamResponseChunk); genai_turn's response comes from there, the request from TLS_WRITE.
 * retcap<0: on_leave reads RAX=ptr/RBX=len but keys the event on the conn saved on_enter. */
{ "TLS_DECRYPT",  "crypto/tls.(*halfConn).decrypt",   AGY_ASYNC, "tls_read",  AGY_OFF,    -1 },
/* AGY_OFF — Conn.Read PARKS on netpoll and resumes on another M, so a gum return hook here is
 * CATASTROPHIC: the return-rewrite window spans the park → 0/15 turns (every one exit-2/empty) in
 * the 2026-07-05 bisect. It's also trampoline-hookable ONLY via full cgocall, not asmcgo (tested):
 * asmcgo (no P handoff) stalled the model turn 0/3 while the baseline was 2/2; full cgocall
 * (entersyscall + P handoff) completed 4/6. The hot netpoll read path needs the P handed off
 * on each fire, so the usual "asmcgo for hot funcs" heuristic is INVERTED here. Left OFF
 * anyway: it yields NO data (the plaintext is the RETURN value the entry-only trampoline
 * can't read; the response is already captured via RESP_CHUNK), while full-cgo costs ~135
 * cgocalls/turn + as many _Gsyscall GC-scan windows on the read path — all for a bare fire marker.
 * retcap<0: on_leave uses the return count (RAX) + the buffer ptr saved on_enter. */
{ "TLS_READ",     "crypto/tls.(*Conn).Read",         AGY_ASYNC, "tls_read",  AGY_OFF,    -1 },
/* RoundTrip(t=RAX, req=RBX) PARKS on the round-trip → cgocall trampoline, AGY_FULLCGO (parking →
 * needs the P handoff; asmcgo would risk a stall). Emits an http_rt marker keyed by the request
 * ptr; the response is a RETURN the trampoline can't read and the request is already captured via
 * TLS_WRITE, so this is a fire/timing marker more than new data. */
{ "HTTP_RT",      "net/http.(*Transport).RoundTrip", AGY_ASYNC, "http_rt",   AGY_FULLCGO, 0 },

/* AGY_OFF — app-layer capture R&D (CPU-only funcs returning []byte). Retired: a gum return hook is
 * GC-unwind-unsafe (see header RETCAP). Moot regardless — the 2026-07-05 bisect confirmed all three
 * fire 0× on agy 1.0.16 (dead jetski model-package paths; the request goes out via the CloudCode
 * client → TLS_WRITE), so a gum return hook here was 100% safe ONLY because it never executes.
 * retcap>0 → the generic on_leave getter branch (emit the RAX/RBX return; 256 skips tiny protos). */
{ "SER_ROOT",     "google3/third_party/jetski/cli/model/model.(*RootModel).Serialize",    AGY_ASYNC, "serialize", AGY_OFF,   1 },
{ "MAR_PROMPT",   "google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON", AGY_ASYNC, "marshal",   AGY_OFF,   1 },
{ "PROTO_MARSHAL","google3/third_party/golang/gogo/protobuf/proto/proto.Marshal",            AGY_ASYNC, "proto_marshal", AGY_OFF, 256 },

/* cgocall-TRAMPOLINE app-boundary hooks (Approach A — the robust general mechanism).
 * The parking targets (SendUserMessage/callbackStreamer.Send): instead of a gum
 * attach (whose return-tracking breaks on park/reschedule) we redirect them through
 * a generated trampoline (cgotrampoline.cpp) + a synthetic moduledata so GC stack-unwind is safe.
 * MODE/retcap are advisory here — the trampoline reads args on the g0 stack and never
 * intercepts the return. Both PARK → AGY_FULLCGO (the P handoff; asmcgo would risk a stall). */
{ "CGT_SEND_USER_MSG", "google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage", AGY_ASYNC, "send_user_msg", AGY_FULLCGO, 0 },
{ "CGT_STREAM_SEND",   "google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send",          AGY_ASYNC, "stream_send",   AGY_FULLCGO, 0 },

/* AGY_OFF — cgocall-trampoline validation probe on the BENIGN os.Getenv. Disabled because
 * os.Getenv is ALSO hooked by SMOKE_GETENV (gum) above; installing both would patch one
 * function entry with a gum inline hook AND a trampoline redirect (overlapping SMC → crash).
 * The trampoline mechanism is exercised by the real trampoline hooks; this validator is redundant. */
{ "CGT_GETENV", "os.Getenv", AGY_ASYNC, "cgt_getenv", AGY_OFF, 0 },

/* AGY_OFF — model-TEXT leaf getters (gemini_coder pipeline): frameless nosplit getters that
 * RETURN the streamed assistant text (on_leave RAX=ptr,RBX=len). Retired: a gum return hook is
 * GC-unwind-unsafe (see header RETCAP), and being frameless they aren't trampoline-hookable
 * either (no prologue to relocate). Moot — the 2026-07-05 bisect confirmed both fire 0× on 1.0.16
 * (old api_server_pb/codeium_common surfaces); the answer is captured at FH_UPDATE (app_response). */
{ "GET_DELTA_CCPA", "google3/third_party/jetski/api_server_pb/api_server_go_proto.(*GetChatMessageResponse).GetDeltaText", AGY_ASYNC, "delta_ccpa", AGY_OFF,   1 },
{ "GET_DELTA_CMPL", "google3/third_party/jetski/codeium_common_pb/codeium_common_go_proto.(*CompletionDelta).GetDeltaText", AGY_ASYNC, "delta_completion", AGY_OFF,   1 },
/* AGY_OFF — RESPONSE getters (assembled assistant text + thinking as Go strings, on_leave
 * RAX=ptr,RBX=len). Retired: a gum return hook is GC-unwind-unsafe (see header RETCAP). The
 * 2026-07-05 bisect found them 30/30 safe but firing 0× on 1.0.16 (this cortex_go_proto surface is
 * dead on the gemini_coder path) — so they'd add nothing even if kept. Thinking, if wanted, is a
 * thought part in the RESP_CHUNK SSE stream, not here. */
{ "RESP_TEXT",     "google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetResponse", AGY_ASYNC, "resp_text",     AGY_OFF,   1 },
{ "RESP_THINKING", "google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetThinking", AGY_ASYNC, "resp_thinking", AGY_OFF,   1 },
{ "RESP_VIEW",     "google3/third_party/jetski/cortex/trajectory/trajectory.(*PlannerResponseStepView).Response",   AGY_ASYNC, "resp_view",     AGY_OFF,   1 },

/* model-TEXT framework choke points (provider-agnostic). On the parking
 * stream-consumer path → cgocall trampoline; AGY_PROC_CGT_ARGS walks their args to
 * locate the accumulated/per-delta assistant text. finalize = full per-turn text. */
{ "FH_FINALIZE", "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).finalizePlannerResponse", AGY_ASYNC, "fh_finalize", AGY_FULLCGO, 0 },
/* THE clean response consumer (Plan 7 probe winner): updateWithStep's RSI arg points
 * at the planner response; the assistant text is a Go string at +0x8/+0x10 — ONE
 * deref, the stable cortex proto layout. agy_cgo_hook decodes it directly to
 * `app_response` (far shallower than AppendStep/OnStepsChanged's 6-deep graphs). */
{ "FH_UPDATE",   "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).updateWithStep",          AGY_ASYNC, "fh_update",   AGY_FULLCGO, 0 },
{ "FH_PROCESS",  "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).processStream",           AGY_ASYNC, "fh_process",  AGY_FULLCGO, 0 },
{ "CORE_PLANSTEP", "google3/third_party/gemini_coder/framework/core/core.createPlannerResponseStep",                                AGY_ASYNC, "core_planstep", AGY_FULLCGO, 0 },
/* consumer-entry hook for the RESPONSE (solves the return-value problem without
 * capturing a return): AppendStep/SetStep take the completed *Step — with the
 * assembled assistant text (GetPlannerResponse→GetResponse) — as an ENTRY arg.
 * Trampoline (they park committing to the trajectory); walk with AGY_PROC_CGT_ARGS. */
{ "TRAJ_APPENDSTEP", "google3/third_party/gemini_coder/framework/core/integration/integration.(*ToolContextTrajectory).AppendStep", AGY_ASYNC, "traj_appendstep", AGY_FULLCGO, 0 },
{ "TRAJ_ADDSTEP",    "google3/third_party/jetski/cortex/traj/traj.(*Trajectory).AddStep",                                             AGY_ASYNC, "traj_addstep",    AGY_FULLCGO, 0 },
{ "TRAJ_ONSTEPS",    "google3/third_party/jetski/cortex/agent_state_component/agent_state_component.(*AgentState).OnStepsChanged",     AGY_ASYNC, "traj_onsteps",    AGY_FULLCGO, 0 },
/* Plan 7 — the commit point one frame above OnStepsChanged (runExecution → AppendStep
 * → observers). Fires on the live --print path (unlike ToolContextTrajectory.AppendStep
 * above). Kept as a documented chain endpoint + stack anchor; the live probe found its
 * *Step text is 6 struct-hops deep (as fragile as OnStepsChanged), so the clean
 * `app_response` decode lives on FH_UPDATE, not here. */
{ "TRAJ_APPENDSTEP_EXEC", "google3/third_party/gemini_coder/framework/executor/executor.(*ExecutionTrajectory).AppendStep",           AGY_ASYNC, "traj_appendstep_exec", AGY_FULLCGO, 0 },

/* CodeAssistClient RPC trace. (*CodeAssistClient).* is agy's single client
 * to the CloudCode backend — each method is one named RPC with typed proto args. They
 * park on the HTTP round-trip → cgocall trampoline. The kind is the RPC label (→ a
 * time-ordered app-level trace); AGY_PROC_STACK adds the call stack, AGY_PROC_CGT_ARGS
 * walks the request proto at entry. StreamGenerateContent is the model turn itself. */
#define CAC "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient)."
{ "RPC_STREAM_GEN",   CAC "StreamGenerateContent",            AGY_ASYNC, "rpc_stream_generate",   AGY_FULLCGO, 0 },
{ "RPC_GEN",          CAC "GenerateContent",                  AGY_ASYNC, "rpc_generate",          AGY_FULLCGO, 0 },
{ "RPC_LOAD_CA",      CAC "FetchLoadCodeAssistResponse",      AGY_ASYNC, "rpc_load_code_assist",  AGY_FULLCGO, 0 },
{ "RPC_USERINFO",     CAC "FetchUserInfo",                    AGY_ASYNC, "rpc_fetch_userinfo",    AGY_FULLCGO, 0 },
{ "RPC_MODELS",       CAC "FetchAvailableModels",             AGY_ASYNC, "rpc_fetch_models",      AGY_FULLCGO, 0 },
{ "RPC_EXPERIMENTS",  CAC "ListExperiments",                  AGY_ASYNC, "rpc_list_experiments",  AGY_FULLCGO, 0 },
{ "RPC_QUOTA",        CAC "RetrieveUserQuotaSummary",         AGY_ASYNC, "rpc_quota",             AGY_FULLCGO, 0 },
{ "RPC_REC_OFFERED",  CAC "RecordConversationOffered",        AGY_ASYNC, "rpc_record_offered",    AGY_FULLCGO, 0 },
{ "RPC_REC_TRAJ",     CAC "RecordTrajectorySegmentAnalytics", AGY_ASYNC, "rpc_record_trajectory", AGY_FULLCGO, 0 },
{ "RPC_WRITE_ACLS",   CAC "WriteTrajectoryACLs",              AGY_ASYNC, "rpc_write_acls",        AGY_FULLCGO, 0 },
#undef CAC
/* response-stream decode probes: the wire genai_turn RESPONSE (restores what TLS_DECRYPT
 * carried before retirement). HTTP/1.1 SSE is pull-based, so the decrypted read is a return
 * value with no entry-arg source; toStreamResponseChunk instead receives each DECODED SSE
 * `data:` line as an entry arg (line.ptr=rax, line.len=rbx) — read on the trampoline in
 * agy_cgo_hook. Parking response path → AGY_FULLCGO. sendUsageDelta carries model id + usage;
 * run with AGY_PROC_CGT_ARGS to walk its args. */
#define CAC2 "google3/third_party/jetski/language_server/code_assist_client/codeassistclient."
{ "RESP_CHUNK",  CAC2 "toStreamResponseChunk",                   AGY_ASYNC, "resp_chunk",  AGY_FULLCGO, 0 },
{ "USAGE_DELTA", CAC2 "(*streamResponseHandler).sendUsageDelta", AGY_ASYNC, "usage_delta", AGY_FULLCGO, 0 },
#undef CAC2
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
