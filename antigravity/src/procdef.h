/* procdef.h — BOOKS, the book table: { "id", MODE, MECH, RETCAP, AGY_SYM("go.symbol") }
 *
 * id      the row's one name: bk() key, agy_stub__/agy_go__ suffix, and its events' kind.
 * MODE    WIRE_ASYNC logs; WIRE_SYNC blocks for a verdict.
 * MECH    AGY_FULLCGO for functions that may park; AGY_ASMCGO (cheaper) only for hot ones that
 *         never park; AGY_OFF not hooked, the row's comment says why.
 * RETCAP  return capture: 0 none, <0 special, >0 min bytes. Return hooks broke Go's GC unwinder,
 *         so every RETCAP != 0 row is AGY_OFF; captures read entry args instead.
 * AGY_SYM the hooked Go symbol, read by symbols/agyelf.py; empty in C++.
 */
#ifndef AGY_PROCDEF_H
#define AGY_PROCDEF_H

#include "wirecap.h"
#include <algorithm>
#include <cstdint>
#include <string_view>

enum agy_mech_t { AGY_OFF = 0, AGY_FULLCGO, AGY_ASMCGO };

struct agy_book {
    std::string_view id;      /* views the row's literal, so NUL-terminated */
    wire_mode_t      mode;
    agy_mech_t       mech;
    int32_t          retcap;
};

#define AGY_SYM(sym)   /* empty; the trailing comma it leaves is legal */

inline constexpr agy_book BOOKS[] = {
/* Liveness smoke: hot, never parks. */
{ "smoke", WIRE_ASYNC, AGY_ASMCGO, 0, AGY_SYM("os.Getenv") },
/* Clean-exit marker, SYNC so it is written before the process exits. */
{ "exit", WIRE_SYNC, AGY_FULLCGO, 0, AGY_SYM("os.Exit") },
/* Conversation id from the opened path; only with AGY_PROC_CONV_ID. */
{ "file_open", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("os.OpenFile") },
/* Answers /proc/self/exe with the real agy path (os.Readlink is inlined, so hook os.readlink). */
{ "readlink_filter", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("os.readlink") },
/* The model request: hot, never parks. */
{ "tls_write", WIRE_ASYNC, AGY_ASMCGO, 0, AGY_SYM("crypto/tls.(*Conn).Write") },
/* Each HTTP/2 response body chunk. */
{ "resp", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("net/http/internal/http2.(*pipe).Write") },
/* Off: the plaintext is a return value; resp_chunk captures the response. */
{ "tls_decrypt", WIRE_ASYNC, AGY_OFF, -1, AGY_SYM("crypto/tls.(*halfConn).decrypt") },
{ "tls_read", WIRE_ASYNC, AGY_OFF, -1, AGY_SYM("crypto/tls.(*Conn).Read") },
/* Round-trip timing marker. */
{ "http_rt", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("net/http.(*Transport).RoundTrip") },
/* Off: return-value getters that never fire on current agy. */
{ "serialize", WIRE_ASYNC, AGY_OFF, 1, AGY_SYM("google3/third_party/jetski/cli/model/model.(*RootModel).Serialize") },
{ "marshal", WIRE_ASYNC, AGY_OFF, 1, AGY_SYM("google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON") },
{ "proto_marshal", WIRE_ASYNC, AGY_OFF, 256, AGY_SYM("google3/third_party/golang/gogo/protobuf/proto/proto.Marshal") },
/* App boundary: the user message in, the streamed reply out. */
{ "send_user_msg", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage") },
{ "stream_send", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send") },
/* Off: smoke already patches os.Getenv. */
{ "cgt_getenv", WIRE_ASYNC, AGY_OFF, 0, AGY_SYM("os.Getenv") },
/* Off: return-value getters that never fire on current agy. */
{ "delta_ccpa", WIRE_ASYNC, AGY_OFF, 1, AGY_SYM("google3/third_party/jetski/api_server_pb/api_server_go_proto.(*GetChatMessageResponse).GetDeltaText") },
{ "delta_completion", WIRE_ASYNC, AGY_OFF, 1, AGY_SYM("google3/third_party/jetski/codeium_common_pb/codeium_common_go_proto.(*CompletionDelta).GetDeltaText") },
{ "resp_text", WIRE_ASYNC, AGY_OFF, 1, AGY_SYM("google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetResponse") },
{ "resp_thinking", WIRE_ASYNC, AGY_OFF, 1, AGY_SYM("google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetThinking") },
{ "resp_view", WIRE_ASYNC, AGY_OFF, 1, AGY_SYM("google3/third_party/jetski/cortex/trajectory/trajectory.(*PlannerResponseStepView).Response") },
/* Response consumers; fh_update decodes the answer as app_response. */
{ "fh_finalize", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).finalizePlannerResponse") },
{ "fh_update", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).updateWithStep") },
{ "fh_process", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).processStream") },
{ "core_planstep", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/oneharness/internal/core/core.createPlannerResponseStep") },
/* Trajectory updates, each taking the completed step as an entry arg. */
{ "traj_appendstep", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/oneharness/internal/core/integration/integration.(*ToolContextTrajectory).AppendStep") },
{ "traj_addstep", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/jetski/cortex/traj/traj.(*Trajectory).AddStep") },
{ "traj_onsteps", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/jetski/cortex/agent_state_component/agent_state_component.(*AgentState).OnStepsChanged") },
{ "traj_appendstep_exec", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM("google3/third_party/gemini_coder/framework/executor/executor.(*ExecutionTrajectory).AppendStep") },
/* CloudCode RPCs, one row per method. */
#define CAC "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient)."
{ "rpc_stream_generate", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC "StreamGenerateContent") },
{ "rpc_generate", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC "GenerateContent") },
{ "rpc_load_code_assist", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC "FetchLoadCodeAssistResponse") },
{ "rpc_fetch_userinfo", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC "FetchUserInfo") },
{ "rpc_fetch_models", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC "FetchAvailableModels") },
{ "rpc_list_experiments", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC "ListExperiments") },
{ "rpc_quota", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC "RetrieveUserQuotaSummary") },
{ "rpc_record_trajectory", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC "RecordTrajectorySegmentAnalytics") },
{ "rpc_write_acls", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC "WriteTrajectoryACLs") },
#undef CAC
/* The response stream: each decoded SSE line, and the usage. */
#define CAC2 "google3/third_party/jetski/language_server/code_assist_client/codeassistclient."
{ "resp_chunk", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC2 "toStreamResponseChunk") },
{ "usage_delta", WIRE_ASYNC, AGY_FULLCGO, 0, AGY_SYM(CAC2 "(*streamResponseHandler).sendUsageDelta") },
#undef CAC2
};

inline constexpr int BK_COUNT = (int)(sizeof(BOOKS) / sizeof(BOOKS[0]));
inline constexpr int BK_SLOTS = (int)std::ranges::count_if(BOOKS, [](auto &b) { return b.mech != AGY_OFF; });

/* Row index by id; an unknown id fails compilation. */
consteval int bk(std::string_view id) {
    for (int i = 0; i < BK_COUNT; i++)
        if (BOOKS[i].id == id) return i;
    throw "unknown hook id";
}

#endif /* AGY_PROCDEF_H */
