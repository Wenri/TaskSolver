/* procdef.h — BOOKS: { "id", MODE, RETCAP, AGY_FULLGO/AGY_ASMGO/AGY_OFF("go.symbol") }
 *
 * id      the row's one name: bk() key, agy_stub__/agy_go__ suffix, and its events' kind.
 * MODE    WIRE_ASYNC logs; WIRE_SYNC blocks for a verdict.
 * RETCAP  return capture: 0 none, <0 special, >0 min bytes. Return books broke Go's GC unwinder,
 *         so every RETCAP != 0 row is AGY_OFF; captures read entry args instead.
 * TARGET  AGY_FULLGO for functions that may park, AGY_ASMGO only for hot ones that never park,
 *         AGY_OFF to disable. agyelf.py also reads the Go symbol, which C++ drops.
 */
#ifndef AGY_PROCDEF_H
#define AGY_PROCDEF_H

#include "wirecap.h"
#include <algorithm>
#include <cstdint>
#include <iterator>
#include <string_view>

enum agy_mech_t { AGY_OFF = 0, AGY_FULLGO, AGY_ASMGO };

struct agy_book {
    std::string_view id;      /* views the row's literal, so NUL-terminated */
    wire_mode_t      mode;
    int32_t          retcap;
    agy_mech_t       mech;
};

#define AGY_FULLGO(sym) AGY_FULLGO
#define AGY_ASMGO(sym)  AGY_ASMGO
#define AGY_OFF(sym)    AGY_OFF

inline constexpr agy_book BOOKS[] = {
/* Liveness smoke: hot, never parks. */
{ "smoke", WIRE_ASYNC, 0, AGY_ASMGO("os.Getenv") },
/* Clean-exit marker, SYNC so it is written before the process exits. */
{ "exit", WIRE_SYNC, 0, AGY_FULLGO("os.Exit") },
/* Conversation id from the opened path; only with AGY_PROC_CONV_ID. */
{ "file_open", WIRE_ASYNC, 0, AGY_FULLGO("os.OpenFile") },
/* Answers /proc/self/exe with the real agy path (os.Readlink is inlined, so book os.readlink). */
{ "readlink_filter", WIRE_ASYNC, 0, AGY_FULLGO("os.readlink") },
/* The model request: hot, never parks. */
{ "tls_write", WIRE_ASYNC, 0, AGY_ASMGO("crypto/tls.(*Conn).Write") },
/* Each HTTP/2 response body chunk. */
{ "resp", WIRE_ASYNC, 0, AGY_FULLGO("net/http/internal/http2.(*pipe).Write") },
/* Off: the plaintext is a return value; resp_chunk captures the response. */
{ "tls_decrypt", WIRE_ASYNC, -1, AGY_OFF("crypto/tls.(*halfConn).decrypt") },
{ "tls_read", WIRE_ASYNC, -1, AGY_OFF("crypto/tls.(*Conn).Read") },
/* Round-trip timing marker. */
{ "http_rt", WIRE_ASYNC, 0, AGY_FULLGO("net/http.(*Transport).RoundTrip") },
/* Off: return-value getters that never fire on current agy. */
{ "serialize", WIRE_ASYNC, 1, AGY_OFF("google3/third_party/jetski/cli/model/model.(*RootModel).Serialize") },
{ "marshal", WIRE_ASYNC, 1, AGY_OFF("google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON") },
{ "proto_marshal", WIRE_ASYNC, 256, AGY_OFF("google3/third_party/golang/gogo/protobuf/proto/proto.Marshal") },
/* App boundary: the user message in, the streamed reply out. */
{ "send_user_msg", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage") },
{ "stream_send", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send") },
/* System steps consumed by either prompt serializer, including agy's own notifications.
 * Entry args carry the Step; repeated history serialization remains observable. */
{ "system_message", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/oneharness/internal/core/serializers/serializers.DefaultSerializer.systemMessageToMessage") },
{ "system_message_legacy", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/jetski/cortex/chatconverters/chatconverters.(*SystemMessageStringConverter).ToOutputString") },
/* Off: smoke already patches os.Getenv. */
{ "cgt_getenv", WIRE_ASYNC, 0, AGY_OFF("os.Getenv") },
/* Off: return-value getters that never fire on current agy. */
{ "delta_ccpa", WIRE_ASYNC, 1, AGY_OFF("google3/third_party/jetski/api_server_pb/api_server_go_proto.(*GetChatMessageResponse).GetDeltaText") },
{ "delta_completion", WIRE_ASYNC, 1, AGY_OFF("google3/third_party/jetski/codeium_common_pb/codeium_common_go_proto.(*CompletionDelta).GetDeltaText") },
{ "resp_text", WIRE_ASYNC, 1, AGY_OFF("google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetResponse") },
{ "resp_thinking", WIRE_ASYNC, 1, AGY_OFF("google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetThinking") },
{ "resp_view", WIRE_ASYNC, 1, AGY_OFF("google3/third_party/jetski/cortex/trajectory/trajectory.(*PlannerResponseStepView).Response") },
/* Response consumers; fh_update decodes the answer as app_response. */
{ "fh_finalize", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).finalizePlannerResponse") },
{ "fh_update", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).updateWithStep") },
{ "fh_process", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).processStream") },
{ "core_planstep", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/oneharness/internal/core/core.createPlannerResponseStep") },
/* Trajectory updates, each taking the completed step as an entry arg. */
{ "traj_appendstep", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/oneharness/internal/core/integration/integration.(*ToolContextTrajectory).AppendStep") },
{ "traj_addstep", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/jetski/cortex/traj/traj.(*Trajectory).AddStep") },
{ "traj_onsteps", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/jetski/cortex/agent_state_component/agent_state_component.(*AgentState).OnStepsChanged") },
{ "traj_appendstep_exec", WIRE_ASYNC, 0, AGY_FULLGO("google3/third_party/gemini_coder/framework/executor/executor.(*ExecutionTrajectory).AppendStep") },
/* CloudCode RPCs, one row per method. */
#define CAC "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient)."
{ "rpc_stream_generate", WIRE_ASYNC, 0, AGY_FULLGO(CAC "StreamGenerateContent") },
{ "rpc_generate", WIRE_ASYNC, 0, AGY_FULLGO(CAC "GenerateContent") },
{ "rpc_load_code_assist", WIRE_ASYNC, 0, AGY_FULLGO(CAC "FetchLoadCodeAssistResponse") },
{ "rpc_fetch_userinfo", WIRE_ASYNC, 0, AGY_FULLGO(CAC "FetchUserInfo") },
{ "rpc_fetch_models", WIRE_ASYNC, 0, AGY_FULLGO(CAC "FetchAvailableModels") },
{ "rpc_list_experiments", WIRE_ASYNC, 0, AGY_FULLGO(CAC "ListExperiments") },
{ "rpc_quota", WIRE_ASYNC, 0, AGY_FULLGO(CAC "RetrieveUserQuotaSummary") },
{ "rpc_record_trajectory", WIRE_ASYNC, 0, AGY_FULLGO(CAC "RecordTrajectorySegmentAnalytics") },
{ "rpc_write_acls", WIRE_ASYNC, 0, AGY_FULLGO(CAC "WriteTrajectoryACLs") },
#undef CAC
/* The response stream: each decoded SSE line, and the usage. */
#define CAC2 "google3/third_party/jetski/language_server/code_assist_client/codeassistclient."
{ "resp_chunk", WIRE_ASYNC, 0, AGY_FULLGO(CAC2 "toStreamResponseChunk") },
{ "usage_delta", WIRE_ASYNC, 0, AGY_FULLGO(CAC2 "(*streamResponseHandler).sendUsageDelta") },
#undef CAC2
};

#undef AGY_FULLGO
#undef AGY_ASMGO
#undef AGY_OFF

inline constexpr int BK_COUNT = (int)std::size(BOOKS);
inline constexpr int BK_STUB_COUNT = (int)std::ranges::count_if(BOOKS, [](auto &b) { return b.mech != AGY_OFF; });

/* Row index by id; an unknown id fails compilation. */
consteval int bk(std::string_view id) {
    for (int i = 0; i < BK_COUNT; i++)
        if (BOOKS[i].id == id) return i;
    throw "unknown book id";
}

#endif /* AGY_PROCDEF_H */
