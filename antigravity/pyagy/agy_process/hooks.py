"""Machine-readable mirror of the C hook table (src/procdef.h).

GENERATED — do not edit by hand. Regenerate with::

    pixi run shim-hooks

`pyagy.HOOKS` is a public introspection surface, so this file is checked in and ships in the
wheel. Stdlib-pure: agy_process imports it inside the instrumented CLI's embedded interpreter.

Each row mirrors one C row:
  id      short tag, the `hk("ID")` key at the C call sites
  symbol  the Go symbol the hook patches
  mode    "async" (log) | "sync" (block for a modify verdict)
  kind    the tag passed to dispatch(kind, stream_id, data)
  mech    "off" (not installed) | "fullcgo" | "asmcgo" — the cgocall trampoline flavour
  retcap  return-capture policy: 0 none, <0 special-cased, >0 min bytes. Every retcap != 0 hook
          is mech="off": the gum return-hook path it needed was retired and deleted, so this is
          the recorded register contract, not live behaviour.
"""

HOOKS = [
    {"id": 'SMOKE_GETENV', "symbol": 'os.Getenv',
     "mode": 'async', "kind": 'smoke', "mech": 'asmcgo', "retcap": 0},
    {"id": 'EXIT', "symbol": 'os.Exit',
     "mode": 'sync', "kind": 'exit', "mech": 'fullcgo', "retcap": 0},
    {"id": 'FILE_OPEN', "symbol": 'os.OpenFile',
     "mode": 'async', "kind": 'file_open', "mech": 'fullcgo', "retcap": 0},
    {"id": 'READLINK_FILTER', "symbol": 'os.readlink',
     "mode": 'async', "kind": 'readlink_filter', "mech": 'fullcgo', "retcap": 0},
    {"id": 'TLS_WRITE', "symbol": 'crypto/tls.(*Conn).Write',
     "mode": 'async', "kind": 'tls_write', "mech": 'asmcgo', "retcap": 0},
    {"id": 'H2_PIPE_WRITE', "symbol": 'net/http/internal/http2.(*pipe).Write',
     "mode": 'async', "kind": 'resp', "mech": 'fullcgo', "retcap": 0},
    {"id": 'TLS_DECRYPT', "symbol": 'crypto/tls.(*halfConn).decrypt',
     "mode": 'async', "kind": 'tls_read', "mech": 'off', "retcap": -1},
    {"id": 'TLS_READ', "symbol": 'crypto/tls.(*Conn).Read',
     "mode": 'async', "kind": 'tls_read', "mech": 'off', "retcap": -1},
    {"id": 'HTTP_RT', "symbol": 'net/http.(*Transport).RoundTrip',
     "mode": 'async', "kind": 'http_rt', "mech": 'fullcgo', "retcap": 0},
    {"id": 'SER_ROOT', "symbol": 'google3/third_party/jetski/cli/model/model.(*RootModel).Serialize',
     "mode": 'async', "kind": 'serialize', "mech": 'off', "retcap": 1},
    {"id": 'MAR_PROMPT', "symbol": 'google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON',
     "mode": 'async', "kind": 'marshal', "mech": 'off', "retcap": 1},
    {"id": 'PROTO_MARSHAL', "symbol": 'google3/third_party/golang/gogo/protobuf/proto/proto.Marshal',
     "mode": 'async', "kind": 'proto_marshal', "mech": 'off', "retcap": 256},
    {"id": 'CGT_SEND_USER_MSG', "symbol": 'google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage',
     "mode": 'async', "kind": 'send_user_msg', "mech": 'fullcgo', "retcap": 0},
    {"id": 'CGT_STREAM_SEND', "symbol": 'google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send',
     "mode": 'async', "kind": 'stream_send', "mech": 'fullcgo', "retcap": 0},
    {"id": 'CGT_GETENV', "symbol": 'os.Getenv',
     "mode": 'async', "kind": 'cgt_getenv', "mech": 'off', "retcap": 0},
    {"id": 'GET_DELTA_CCPA', "symbol": 'google3/third_party/jetski/api_server_pb/api_server_go_proto.(*GetChatMessageResponse).GetDeltaText',
     "mode": 'async', "kind": 'delta_ccpa', "mech": 'off', "retcap": 1},
    {"id": 'GET_DELTA_CMPL', "symbol": 'google3/third_party/jetski/codeium_common_pb/codeium_common_go_proto.(*CompletionDelta).GetDeltaText',
     "mode": 'async', "kind": 'delta_completion', "mech": 'off', "retcap": 1},
    {"id": 'RESP_TEXT', "symbol": 'google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetResponse',
     "mode": 'async', "kind": 'resp_text', "mech": 'off', "retcap": 1},
    {"id": 'RESP_THINKING', "symbol": 'google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetThinking',
     "mode": 'async', "kind": 'resp_thinking', "mech": 'off', "retcap": 1},
    {"id": 'RESP_VIEW', "symbol": 'google3/third_party/jetski/cortex/trajectory/trajectory.(*PlannerResponseStepView).Response',
     "mode": 'async', "kind": 'resp_view', "mech": 'off', "retcap": 1},
    {"id": 'FH_FINALIZE', "symbol": 'google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).finalizePlannerResponse',
     "mode": 'async', "kind": 'fh_finalize', "mech": 'fullcgo', "retcap": 0},
    {"id": 'FH_UPDATE', "symbol": 'google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).updateWithStep',
     "mode": 'async', "kind": 'fh_update', "mech": 'fullcgo', "retcap": 0},
    {"id": 'FH_PROCESS', "symbol": 'google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).processStream',
     "mode": 'async', "kind": 'fh_process', "mech": 'fullcgo', "retcap": 0},
    {"id": 'CORE_PLANSTEP', "symbol": 'google3/third_party/gemini_coder/framework/core/core.createPlannerResponseStep',
     "mode": 'async', "kind": 'core_planstep', "mech": 'fullcgo', "retcap": 0},
    {"id": 'TRAJ_APPENDSTEP', "symbol": 'google3/third_party/gemini_coder/framework/core/integration/integration.(*ToolContextTrajectory).AppendStep',
     "mode": 'async', "kind": 'traj_appendstep', "mech": 'fullcgo', "retcap": 0},
    {"id": 'TRAJ_ADDSTEP', "symbol": 'google3/third_party/jetski/cortex/traj/traj.(*Trajectory).AddStep',
     "mode": 'async', "kind": 'traj_addstep', "mech": 'fullcgo', "retcap": 0},
    {"id": 'TRAJ_ONSTEPS', "symbol": 'google3/third_party/jetski/cortex/agent_state_component/agent_state_component.(*AgentState).OnStepsChanged',
     "mode": 'async', "kind": 'traj_onsteps', "mech": 'fullcgo', "retcap": 0},
    {"id": 'TRAJ_APPENDSTEP_EXEC', "symbol": 'google3/third_party/gemini_coder/framework/executor/executor.(*ExecutionTrajectory).AppendStep',
     "mode": 'async', "kind": 'traj_appendstep_exec', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_STREAM_GEN', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).StreamGenerateContent',
     "mode": 'async', "kind": 'rpc_stream_generate', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_GEN', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).GenerateContent',
     "mode": 'async', "kind": 'rpc_generate', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_LOAD_CA', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).FetchLoadCodeAssistResponse',
     "mode": 'async', "kind": 'rpc_load_code_assist', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_USERINFO', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).FetchUserInfo',
     "mode": 'async', "kind": 'rpc_fetch_userinfo', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_MODELS', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).FetchAvailableModels',
     "mode": 'async', "kind": 'rpc_fetch_models', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_EXPERIMENTS', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).ListExperiments',
     "mode": 'async', "kind": 'rpc_list_experiments', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_QUOTA', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).RetrieveUserQuotaSummary',
     "mode": 'async', "kind": 'rpc_quota', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_REC_OFFERED', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).RecordConversationOffered',
     "mode": 'async', "kind": 'rpc_record_offered', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_REC_TRAJ', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).RecordTrajectorySegmentAnalytics',
     "mode": 'async', "kind": 'rpc_record_trajectory', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RPC_WRITE_ACLS', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).WriteTrajectoryACLs',
     "mode": 'async', "kind": 'rpc_write_acls', "mech": 'fullcgo', "retcap": 0},
    {"id": 'RESP_CHUNK', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.toStreamResponseChunk',
     "mode": 'async', "kind": 'resp_chunk', "mech": 'fullcgo', "retcap": 0},
    {"id": 'USAGE_DELTA', "symbol": 'google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*streamResponseHandler).sendUsageDelta',
     "mode": 'async', "kind": 'usage_delta', "mech": 'fullcgo', "retcap": 0},
]

#: kinds the Python layer DERIVES rather than receiving straight from a hook.
DERIVED_KINDS = ("genai_turn", "h2msg", "conversation_id", "callstack", "app_response")


def by_mech(mech):
    """Rows whose install mechanism is `mech` ("off" / "fullcgo" / "asmcgo")."""
    return [h for h in HOOKS if h["mech"] == mech]


def by_kind(kind):
    """Rows emitting `kind` (several hooks can share one kind)."""
    return [h for h in HOOKS if h["kind"] == kind]


def enabled_hooks():
    """Rows actually installed at runtime (everything not mech="off")."""
    return [h for h in HOOKS if h["mech"] != "off"]


def sync_capable():
    """Rows whose dispatch can return replacement bytes (mode="sync")."""
    return [h for h in HOOKS if h["mode"] == "sync"]
