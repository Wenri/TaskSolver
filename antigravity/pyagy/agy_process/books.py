"""Machine-readable mirror of BOOKS, the book table in src/procdef.h.

GENERATED — do not edit by hand. Regenerate with::

    pixi run shim-books

`pyagy.BOOKS` is a public introspection surface, so this file is checked in and ships in the
wheel. Stdlib-pure: agy_process imports it inside the instrumented CLI's embedded interpreter.

Each row mirrors one C row:
  id      the row's name, the `bk("id")` key at the C call sites
  symbol  the Go symbol the book patches
  mode    "async" (log) | "sync" (block for a modify verdict)
  kind    the same name, as the tag passed to dispatch(kind, stream_id, data)
  mech    "off" (not installed) | "fullcgo" | "asmcgo" — the cgocall trampoline flavour
  retcap  return-capture policy (0 none, <0 special, >0 min bytes); every retcap != 0 row is off
"""

BOOKS = [
    {"id": 'smoke', "symbol": 'os.Getenv',
     "mode": 'async', "kind": 'smoke', "mech": 'asmcgo', "retcap": 0},
    {"id": 'exit', "symbol": 'os.Exit',
     "mode": 'sync', "kind": 'exit', "mech": 'fullcgo', "retcap": 0},
    {"id": 'file_open', "symbol": 'os.OpenFile',
     "mode": 'async', "kind": 'file_open', "mech": 'fullcgo', "retcap": 0},
    {"id": 'readlink_filter', "symbol": 'os.readlink',
     "mode": 'async', "kind": 'readlink_filter', "mech": 'fullcgo', "retcap": 0},
    {"id": 'tls_write', "symbol": 'crypto/tls.(*Conn).Write',
     "mode": 'async', "kind": 'tls_write', "mech": 'asmcgo', "retcap": 0},
    {"id": 'resp', "symbol": 'net/http/internal/http2.(*pipe).Write',
     "mode": 'async', "kind": 'resp', "mech": 'fullcgo', "retcap": 0},
    {"id": 'tls_decrypt', "symbol": 'crypto/tls.(*halfConn).decrypt',
     "mode": 'async', "kind": 'tls_decrypt', "mech": 'off', "retcap": -1},
    {"id": 'tls_read', "symbol": 'crypto/tls.(*Conn).Read',
     "mode": 'async', "kind": 'tls_read', "mech": 'off', "retcap": -1},
    {"id": 'http_rt', "symbol": 'net/http.(*Transport).RoundTrip',
     "mode": 'async', "kind": 'http_rt', "mech": 'fullcgo', "retcap": 0},
    {"id": 'serialize', "symbol": 'google3/third_party/jetski/cli/model/model.(*RootModel).Serialize',
     "mode": 'async', "kind": 'serialize', "mech": 'off', "retcap": 1},
    {"id": 'marshal', "symbol": 'google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON',
     "mode": 'async', "kind": 'marshal', "mech": 'off', "retcap": 1},
    {"id": 'proto_marshal', "symbol": 'google3/third_party/golang/gogo/protobuf/proto/proto.Marshal',
     "mode": 'async', "kind": 'proto_marshal', "mech": 'off', "retcap": 256},
    {"id": 'send_user_msg', "symbol": 'google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage',
     "mode": 'async', "kind": 'send_user_msg', "mech": 'fullcgo', "retcap": 0},
    {"id": 'stream_send', "symbol": 'google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send',
     "mode": 'async', "kind": 'stream_send', "mech": 'fullcgo', "retcap": 0},
    {"id": 'cgt_getenv', "symbol": 'os.Getenv',
     "mode": 'async', "kind": 'cgt_getenv', "mech": 'off', "retcap": 0},
    {"id": 'delta_ccpa', "symbol": 'google3/third_party/jetski/api_server_pb/api_server_go_proto.(*GetChatMessageResponse).GetDeltaText',
     "mode": 'async', "kind": 'delta_ccpa', "mech": 'off', "retcap": 1},
    {"id": 'delta_completion', "symbol": 'google3/third_party/jetski/codeium_common_pb/codeium_common_go_proto.(*CompletionDelta).GetDeltaText',
     "mode": 'async', "kind": 'delta_completion', "mech": 'off', "retcap": 1},
    {"id": 'resp_text', "symbol": 'google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetResponse',
     "mode": 'async', "kind": 'resp_text', "mech": 'off', "retcap": 1},
    {"id": 'resp_thinking', "symbol": 'google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetThinking',
     "mode": 'async', "kind": 'resp_thinking', "mech": 'off', "retcap": 1},
    {"id": 'resp_view', "symbol": 'google3/third_party/jetski/cortex/trajectory/trajectory.(*PlannerResponseStepView).Response',
     "mode": 'async', "kind": 'resp_view', "mech": 'off', "retcap": 1},
    {"id": 'fh_finalize', "symbol": 'google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).finalizePlannerResponse',
     "mode": 'async', "kind": 'fh_finalize', "mech": 'fullcgo', "retcap": 0},
    {"id": 'fh_update', "symbol": 'google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).updateWithStep',
     "mode": 'async', "kind": 'fh_update', "mech": 'fullcgo', "retcap": 0},
    {"id": 'fh_process', "symbol": 'google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).processStream',
     "mode": 'async', "kind": 'fh_process', "mech": 'fullcgo', "retcap": 0},
    {"id": 'core_planstep', "symbol": 'google3/third_party/oneharness/internal/core/core.createPlannerResponseStep',
     "mode": 'async', "kind": 'core_planstep', "mech": 'fullcgo', "retcap": 0},
    {"id": 'traj_appendstep', "symbol": 'google3/third_party/oneharness/internal/core/integration/integration.(*ToolContextTrajectory).AppendStep',
     "mode": 'async', "kind": 'traj_appendstep', "mech": 'fullcgo', "retcap": 0},
    {"id": 'traj_addstep', "symbol": 'google3/third_party/jetski/cortex/traj/traj.(*Trajectory).AddStep',
     "mode": 'async', "kind": 'traj_addstep', "mech": 'fullcgo', "retcap": 0},
    {"id": 'traj_onsteps', "symbol": 'google3/third_party/jetski/cortex/agent_state_component/agent_state_component.(*AgentState).OnStepsChanged',
     "mode": 'async', "kind": 'traj_onsteps', "mech": 'fullcgo', "retcap": 0},
    {"id": 'traj_appendstep_exec', "symbol": 'google3/third_party/gemini_coder/framework/executor/executor.(*ExecutionTrajectory).AppendStep',
     "mode": 'async', "kind": 'traj_appendstep_exec', "mech": 'fullcgo', "retcap": 0},
    {"id": 'rpc_stream_generate', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).StreamGenerateContent',
     "mode": 'async', "kind": 'rpc_stream_generate', "mech": 'fullcgo', "retcap": 0},
    {"id": 'rpc_generate', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).GenerateContent',
     "mode": 'async', "kind": 'rpc_generate', "mech": 'fullcgo', "retcap": 0},
    {"id": 'rpc_load_code_assist', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).FetchLoadCodeAssistResponse',
     "mode": 'async', "kind": 'rpc_load_code_assist', "mech": 'fullcgo', "retcap": 0},
    {"id": 'rpc_fetch_userinfo', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).FetchUserInfo',
     "mode": 'async', "kind": 'rpc_fetch_userinfo', "mech": 'fullcgo', "retcap": 0},
    {"id": 'rpc_fetch_models', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).FetchAvailableModels',
     "mode": 'async', "kind": 'rpc_fetch_models', "mech": 'fullcgo', "retcap": 0},
    {"id": 'rpc_list_experiments', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).ListExperiments',
     "mode": 'async', "kind": 'rpc_list_experiments', "mech": 'fullcgo', "retcap": 0},
    {"id": 'rpc_quota', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).RetrieveUserQuotaSummary',
     "mode": 'async', "kind": 'rpc_quota', "mech": 'fullcgo', "retcap": 0},
    {"id": 'rpc_record_trajectory', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).RecordTrajectorySegmentAnalytics',
     "mode": 'async', "kind": 'rpc_record_trajectory', "mech": 'fullcgo', "retcap": 0},
    {"id": 'rpc_write_acls', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).WriteTrajectoryACLs',
     "mode": 'async', "kind": 'rpc_write_acls', "mech": 'fullcgo', "retcap": 0},
    {"id": 'resp_chunk', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.toStreamResponseChunk',
     "mode": 'async', "kind": 'resp_chunk', "mech": 'fullcgo', "retcap": 0},
    {"id": 'usage_delta', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*streamResponseHandler).sendUsageDelta',
     "mode": 'async', "kind": 'usage_delta', "mech": 'fullcgo', "retcap": 0},
]

#: kinds the Python layer DERIVES rather than receiving straight from a book.
DERIVED_KINDS = ("genai_turn", "h2msg", "conversation_id", "callstack", "app_response")


def by_mech(mech):
    """Rows whose install mechanism is `mech` ("off" / "fullcgo" / "asmcgo")."""
    return [h for h in BOOKS if h["mech"] == mech]


def by_kind(kind):
    """Rows emitting `kind`."""
    return [h for h in BOOKS if h["kind"] == kind]


def enabled_books():
    """Rows actually installed at runtime (everything not mech="off")."""
    return [h for h in BOOKS if h["mech"] != "off"]


def sync_capable():
    """Rows whose dispatch can return replacement bytes (mode="sync")."""
    return [h for h in BOOKS if h["mode"] == "sync"]
