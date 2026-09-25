#!/usr/bin/env python3
"""Offline Claude Agent SDK integration tests; no CLI, credentials or network.

Run with ``python test_scripts/test_claude_sdk.py``. The fake SDK supplies protocol
messages and records lifecycle events, while the real wrapper and TaskSolver
adapter handle options, response parsing, sessions and multimodal prompts.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Final
import unittest
from unittest.mock import patch

_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT / "claude"), str(_ROOT / "claude" / "vendor" / "sdk" / "src"),
               str(_ROOT / "codex"), str(_ROOT)]

from PIL import Image

from pyclaude import client
from pyclaude.model import ClaudeAgentModel
from tasksolver.agent import Agent
from tasksolver.cli_backend import CLIBackendModel
from tasksolver.common import ParsedAnswer, Question, TaskSpec
from tasksolver.exceptions import GPTMaxTriesExceededException, GPTOutputParseException
from tasksolver.keychain import KeyChain


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = "test-signature"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-test-model"
    parent_tool_use_id: str | None = None
    error: str | None = None


@dataclass
class UserMessage:
    content: str | list
    parent_tool_use_id: str | None = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict


@dataclass
class ResultMessage:
    subtype: str = "success"
    duration_ms: int = 1
    duration_api_ms: int = 1
    is_error: bool = False
    num_turns: int = 1
    session_id: str = "session-one"
    total_cost_usd: float | None = 0.02
    usage: dict = field(default_factory=lambda: {"input_tokens": 11, "output_tokens": 3})
    result: str | None = "4"
    structured_output: object = None


class FakeOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeSDK:
    """A stream script for each connection, with observable cleanup and options."""

    def __init__(self, *scripts):
        self.scripts: Final = list(scripts)
        self.calls: Final = []
        self.entered = 0
        self.exited = 0
        self.cancelled = 0
        self.options: Final = []
        owner: Final = self

        class FakeClient:
            def __init__(self, *, options):
                self.options: Final = options
                self.script: Final = owner.scripts.pop(0)
                owner.options.append(options)

            async def __aenter__(self):
                owner.entered += 1
                return self

            async def __aexit__(self, *_args):
                owner.exited += 1

            async def query(self, prompt, **kwargs):
                if hasattr(prompt, "__aiter__"):
                    content = [item async for item in prompt]
                else:
                    content = prompt
                owner.calls.append((content, kwargs))

            async def receive_response(self):
                try:
                    for event in self.script:
                        if event == "wait":
                            await asyncio.sleep(60)
                        elif isinstance(event, BaseException):
                            raise event
                        else:
                            yield event
                except asyncio.CancelledError:
                    owner.cancelled += 1
                    raise

        self.module: Final = SimpleNamespace(
            ClaudeAgentOptions=FakeOptions, ClaudeSDKClient=FakeClient,
            AssistantMessage=AssistantMessage, UserMessage=UserMessage,
            SystemMessage=SystemMessage, ResultMessage=ResultMessage,
            TextBlock=TextBlock, ThinkingBlock=ThinkingBlock,
            ToolUseBlock=ToolUseBlock, ToolResultBlock=ToolResultBlock,
        )

    def install(self):
        return patch.object(client, "_load_sdk", return_value=self.module)


class NumberAnswer(ParsedAnswer):
    def __init__(self, value):
        self.value: Final = value

    @classmethod
    def parser(cls, raw):
        try:
            return cls(int(raw))
        except ValueError as exc:
            raise GPTOutputParseException("Expected an integer") from exc

    def __str__(self):
        return str(self.value)


def number_task():
    return TaskSpec("math", "Return an integer.", NumberAnswer,
                    lambda *_: Question([]), lambda *_: True)


class ClaudeSDKTests(unittest.TestCase):
    def test_result_is_authoritative_and_preserves_metadata(self):
        sdk: Final = FakeSDK([
            AssistantMessage([TextBlock("99"), ThinkingBlock("Visible thought")]),
            AssistantMessage([TextBlock("100")], parent_tool_use_id="child-agent"),
            ResultMessage(result="4"),
        ])
        with sdk.install():
            response: Final = client.ask("2 + 2")
        self.assertEqual(response.text, "4")
        self.assertEqual(response.session_id, "session-one")
        self.assertEqual(response.usage, {"input_tokens": 11, "output_tokens": 3})
        self.assertEqual(response.total_cost_usd, 0.02)
        self.assertEqual(response.exit_status, 0)
        self.assertFalse(response.timed_out)
        self.assertIn("Visible thought", response.thoughts)
        self.assertEqual(sdk.entered, sdk.exited)

    def test_no_terminal_result_rejects_intermediate_answer(self):
        sdk: Final = FakeSDK([AssistantMessage([TextBlock("99")])])
        with sdk.install(), self.assertRaises(client.ClaudeAgentError) as raised:
            client.ask("2 + 2")
        self.assertIsNotNone(raised.exception.response)
        self.assertEqual(raised.exception.response.exit_status, 1)
        self.assertEqual(sdk.exited, 1)

    def test_error_flag_overrides_success_subtype(self):
        sdk: Final = FakeSDK([ResultMessage(subtype="success", is_error=True,
                                           result="Authentication failed")])
        with sdk.install(), self.assertRaises(client.ClaudeAgentError) as raised:
            client.ask("2 + 2")
        self.assertTrue(raised.exception.response.result["is_error"])
        self.assertEqual(raised.exception.response.session_id, "session-one")
        self.assertEqual(sdk.exited, 1)

    def test_error_subtype_rejects_partial_reply(self):
        sdk: Final = FakeSDK([
            AssistantMessage([TextBlock("4")]),
            ResultMessage(subtype="error_max_turns", is_error=False, result=None),
        ])
        with sdk.install(), self.assertRaises(client.ClaudeAgentError):
            client.ask("2 + 2")

    def test_explicit_empty_result_does_not_fall_back_to_progress(self):
        sdk: Final = FakeSDK([
            AssistantMessage([TextBlock("Working on it")]), ResultMessage(result=""),
        ])
        with sdk.install():
            response: Final = client.ask("2 + 2")
        self.assertEqual(response.text, "")

    def test_missing_result_text_uses_latest_main_assistant_message(self):
        sdk: Final = FakeSDK([
            AssistantMessage([TextBlock("Searching")]),
            AssistantMessage([TextBlock("4")]),
            AssistantMessage([TextBlock("unrelated")], parent_tool_use_id="child-agent"),
            ResultMessage(result=None),
        ])
        with sdk.install():
            response: Final = client.ask("2 + 2")
        self.assertEqual(response.text, "4")

    def test_timeout_cancels_stream_and_closes_client(self):
        sdk: Final = FakeSDK([AssistantMessage([TextBlock("Working")]), "wait"])
        with sdk.install(), self.assertRaises(TimeoutError) as raised:
            client.ask("2 + 2", timeout=0.02)
        self.assertTrue(raised.exception.response.timed_out)
        self.assertEqual(raised.exception.response.exit_status, 1)
        self.assertEqual(sdk.cancelled, 1)
        self.assertEqual(sdk.exited, 1)

    def test_transport_failure_closes_client(self):
        sdk: Final = FakeSDK([RuntimeError("transport disconnected")])
        with sdk.install(), self.assertRaisesRegex(RuntimeError, "transport disconnected"):
            client.ask("2 + 2")
        self.assertEqual(sdk.exited, 1)

    def test_async_api(self):
        sdk: Final = FakeSDK([ResultMessage(result="4")])
        with sdk.install():
            response: Final = asyncio.run(client.ask_async("2 + 2"))
        self.assertEqual(response.text, "4")

    def test_sync_api_inside_running_event_loop(self):
        sdk: Final = FakeSDK([ResultMessage(result="4")])

        async def notebook_cell():
            return client.ask("2 + 2")

        with sdk.install():
            response: Final = asyncio.run(notebook_cell())
        self.assertEqual(response.text, "4")
        self.assertEqual(sdk.exited, 1)

    def test_independent_samples(self):
        sdk: Final = FakeSDK([ResultMessage(result="3")], [ResultMessage(result="4")])
        with sdk.install():
            responses: Final = client.ask_many("2 + 2", 2)
        self.assertCountEqual([response.text for response in responses], ["3", "4"])
        self.assertEqual(sdk.exited, 2)
        self.assertTrue(all(getattr(option, "resume", None) is None for option in sdk.options))

    def test_parallel_samples_cannot_share_native_session(self):
        sdk: Final = FakeSDK()
        for kwargs in ({"session_id": "shared"}, {"sdk_options": {"resume": "shared"}},
                       {"sdk_options": {"continue_conversation": True}}):
            with self.subTest(kwargs=kwargs), sdk.install(), self.assertRaises(ValueError):
                client.ask_many("hello", 2, **kwargs)
        self.assertEqual(sdk.entered, 0)

    def test_session_resume_and_close(self):
        sdk: Final = FakeSDK(
            [ResultMessage(result="4", session_id="native-first")],
            [ResultMessage(result="8", session_id="native-second")],
        )
        with sdk.install():
            with client.Session() as session:
                first: Final = session.ask("2 + 2")
                self.assertEqual(session.session_id, "native-first")
                second: Final = session.ask("Double it")
                self.assertEqual(session.session_id, "native-second")
                self.assertEqual(session.history(), [first, second])
            with self.assertRaises(RuntimeError):
                session.ask("After close")
        self.assertIsNone(getattr(sdk.options[0], "resume", None))
        self.assertEqual(sdk.options[1].resume, "native-first")
        self.assertEqual(sdk.exited, 2)

    def test_failed_turn_retains_session_for_explicit_followup(self):
        sdk: Final = FakeSDK(
            [ResultMessage(is_error=True, result="Turn limit", session_id="failed-session")],
            [ResultMessage(result="4", session_id="failed-session")],
        )
        with sdk.install(), client.Session() as session:
            with self.assertRaises(client.ClaudeAgentError):
                session.ask("2 + 2")
            self.assertEqual(session.session_id, "failed-session")
            self.assertEqual(session.ask("Continue").text, "4")
        self.assertEqual(sdk.options[1].resume, "failed-session")

    def test_session_timeout_retains_discovered_resume_id(self):
        sdk: Final = FakeSDK(
            [SystemMessage("init", {"session_id": "interrupted-session"}), "wait"],
            [ResultMessage(result="4", session_id="interrupted-session")],
        )
        with sdk.install(), client.Session() as session:
            with self.assertRaises(TimeoutError) as raised:
                session.ask("slow turn", timeout=0.02)
            self.assertEqual(raised.exception.response.session_id, "interrupted-session")
            self.assertTrue(session.history()[0].timed_out)
            self.assertTrue(session.history()[0].metadata()["timed_out"])
            self.assertEqual(session.history()[0].metadata()["exit_status"], 1)
            self.assertEqual(session.session_id, "interrupted-session")
            self.assertEqual(session.ask("Continue").text, "4")
        self.assertEqual(sdk.options[1].resume, "interrupted-session")
        self.assertEqual(sdk.exited, 2)

    def test_session_rejects_overlap_and_recovers_after_cancellation(self):
        sdk: Final = FakeSDK(["wait"], [ResultMessage(result="4")])

        async def use_session():
            async with client.Session() as session:
                pending: Final = asyncio.create_task(session.ask_async("slow turn"))
                try:
                    async with asyncio.timeout(1):
                        while not sdk.entered:
                            await asyncio.sleep(0)
                    with self.assertRaisesRegex(RuntimeError, "in progress"):
                        await session.ask_async("overlap")
                    with self.assertRaisesRegex(RuntimeError, "in progress"):
                        session.close()
                finally:
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                self.assertEqual((await session.ask_async("new turn")).text, "4")

        with sdk.install():
            asyncio.run(use_session())
        self.assertEqual(sdk.exited, 2)

    def test_key_and_environment_are_scoped_to_sdk(self):
        sdk: Final = FakeSDK([ResultMessage()])
        previous: Final = os.environ.get("ANTHROPIC_API_KEY")
        extra: Final = {"CUSTOM_TEST_VAR": "value"}
        with sdk.install():
            response: Final = client.ask("hello", api_key="test-secret", extra_env=extra)
        self.assertEqual(sdk.options[0].env["ANTHROPIC_API_KEY"], "test-secret")
        self.assertEqual(sdk.options[0].env["CUSTOM_TEST_VAR"], "value")
        self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), previous)
        self.assertEqual(extra, {"CUSTOM_TEST_VAR": "value"})
        self.assertNotIn("test-secret", repr(response.request))
        self.assertNotIn("test-secret", repr(response.metadata()))

    def test_mcp_options_preserve_caller_and_expose_requested_tools(self):
        sdk: Final = FakeSDK([ResultMessage()])
        servers: Final = {"math": {"command": "python", "args": ["server.py"],
                                  "env": {"PORT": "1234"}}}
        original: Final = copy.deepcopy(servers)
        with sdk.install():
            client.ask("hello", mcp_servers=servers)
        self.assertEqual(servers, original)
        configured: Final = sdk.options[0].mcp_servers["math"]
        if os.name == "posix":
            self.assertEqual(configured["command"], "/usr/bin/env")
            self.assertEqual(configured["args"], ["-u", "PYTHONHOME", "python", "server.py"])
        else:
            self.assertEqual(configured["command"], "python")
        self.assertNotIn("mcp__math__*", sdk.options[0].allowed_tools)
        self.assertIn("Read", sdk.options[0].tools)
        self.assertTrue(sdk.options[0].strict_mcp_config)

    def test_in_process_mcp_server_keeps_live_instance(self):
        class LiveServer:
            def __deepcopy__(self, _memo):
                raise TypeError("Live servers cannot be copied")

        instance: Final = LiveServer()
        servers: Final = {"local": {"type": "sdk", "name": "local", "instance": instance}}
        sdk: Final = FakeSDK([ResultMessage()])
        with sdk.install():
            client.ask("hello", mcp_servers=servers)
        self.assertIs(sdk.options[0].mcp_servers["local"]["instance"], instance)

    def test_tool_permissions_are_separate_from_tool_selection(self):
        sdk: Final = FakeSDK([ResultMessage()], [ResultMessage()])
        explicit: Final = ClaudeAgentModel(task=number_task(), tools=("Read",),
                                           allowed_tools=["Read", "mcp__math__*"])
        unrestricted: Final = ClaudeAgentModel(task=number_task(), allowed_tools=None)
        with sdk.install():
            explicit.run_once(Question(["2 + 2"]))
            unrestricted.run_once(Question(["2 + 2"]))
        self.assertEqual(sdk.options[0].tools, ["Read"])
        self.assertEqual(sdk.options[0].allowed_tools, ["Read", "mcp__math__*"])
        self.assertIsNone(sdk.options[1].tools)
        self.assertEqual(sdk.options[1].allowed_tools, [])

    def test_multimodal_payload_and_canonical_model_result(self):
        sdk: Final = FakeSDK([
            AssistantMessage([ThinkingBlock("Visible thought")]), ResultMessage(result="4"),
        ])
        model: Final = ClaudeAgentModel(task=number_task())
        picture: Final = Image.new("RGB", (3, 2), color="red")
        with sdk.install():
            answer, raw, metadata, payload = model.run_once(Question(["How many?", picture]))
        self.assertEqual(answer.value, 4)
        self.assertEqual(raw, {"role": "assistant", "content": "4"})
        self.assertEqual(answer.llm_response_metadata, metadata[0])
        self.assertIs(answer.request_payload, payload)
        self.assertIn("Visible thought", answer.explicit_reasoning_output)
        self.assertEqual(metadata[0]["session_id"], "session-one")
        blocks: Final = payload["prompt"]
        self.assertEqual(sdk.calls[0][0][0]["message"]["content"], blocks)
        self.assertIn("Return an integer.", "\n".join(
            block["text"] for block in blocks if block["type"] == "text"))
        images: Final = [block for block in blocks if block["type"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["source"]["media_type"], "image/png")
        image_data: Final = base64.b64decode(images[0]["source"]["data"])
        with Image.open(io.BytesIO(image_data)) as decoded:
            self.assertEqual(decoded.size, (3, 2))

    def test_parser_retry_and_exhaustion_keep_context(self):
        sdk: Final = FakeSDK([ResultMessage(result="bad")], [ResultMessage(result="4")])
        model: Final = ClaudeAgentModel(task=number_task())
        with sdk.install():
            response: Final = model.run_once(Question(["2 + 2"]), max_tries=1)
        self.assertEqual(response[0].value, 4)
        self.assertEqual(sdk.exited, 2)

        failure: Final = FakeSDK([ResultMessage(result="bad")])
        with failure.install(), self.assertRaises(GPTMaxTriesExceededException) as raised:
            model.run_once(Question(["2 + 2"]), max_tries=0)
        self.assertEqual(raised.exception.raw_response["content"], "bad")
        self.assertEqual(raised.exception.response_metadata[0]["session_id"], "session-one")
        self.assertIn("prompt", raised.exception.request_payload)

    def test_agent_claude_aliases_and_credentials(self):
        task: Final = number_task()
        for alias, expected in (
            ("claude-code", None), ("claude-agent", None),
            ("claude-code-sonnet", "sonnet"),
            ("claude-agent-opus", "opus"),
            ("claude-code-haiku", "claude-haiku-4-5"),
            ("claude-agent-sonnet-4-6", "claude-sonnet-4-6"),
            ("claude-code-opus-4-7", "claude-opus-4-7"),
        ):
            with self.subTest(alias=alias):
                model = Agent("test-key", task, vision_model=alias).visual_interface
                self.assertIsInstance(model, ClaudeAgentModel)
                self.assertEqual(model.model, expected)
                self.assertEqual(model.api_key, "test-key")

        keys: Final = KeyChain().add_key("claude", "keychain-value")
        keyed: Final = Agent(keys, task, vision_model="claude-code").visual_interface
        self.assertEqual(keyed.api_key, "keychain-value")
        absent: Final = Agent("missing/credential.txt", task,
                              vision_model="claude-agent").visual_interface
        self.assertIsNone(absent.api_key)
        mcp: Final = {"test": {"command": "python"}}
        scoped: Final = Agent(None, task, vision_model="claude-agent",
                              workspace="/tmp/example", mcp_servers=mcp).visual_interface
        self.assertEqual(scoped.workspace, "/tmp/example")
        self.assertEqual(scoped.mcp_servers, mcp)

    def test_agent_rejects_invalid_claude_aliases(self):
        for alias in ("claude-code-", "claude-agent-", "claude-code-4-6"):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                Agent(None, number_task(), vision_model=alias)

    def test_http_claude_alias_keeps_http_adapter(self):
        with patch("tasksolver.claude.ClaudeModel") as constructor:
            agent: Final = Agent("http-key", number_task(), vision_model="claude-sonnet-4-6")
        self.assertIs(agent.visual_interface, constructor.return_value)
        self.assertEqual(constructor.call_args.args[0], "http-key")
        self.assertEqual(constructor.call_args.kwargs["model"], "claude-sonnet-4-6")

    def test_codex_transport_aliases(self):
        class CapturedCodex(CLIBackendModel):
            def __init__(self, api_key, task, **kwargs):
                super().__init__(api_key=api_key, task=task, model=kwargs.pop("model"))
                self.options: Final = kwargs

        with patch("pycodex.CodexModel", CapturedCodex):
            for alias, transport, model in (
                ("codex", "sdk", None),
                ("codex-gpt-test", "sdk", "gpt-test"),
                ("codex-sdk", "sdk", None),
                ("codex-sdk-gpt-test", "sdk", "gpt-test"),
            ):
                with self.subTest(alias=alias):
                    adapter = Agent(None, number_task(), vision_model=alias).visual_interface
                    self.assertEqual(adapter.model, model)
                    self.assertEqual(adapter.options["transport"], transport)
            with self.assertRaises(ValueError):
                Agent(None, number_task(), vision_model="codex-sdk-")

    def test_real_sdk_subprocess_protocol_and_timeout_cleanup(self):
        try:
            import claude_agent_sdk
        except ImportError:
            self.skipTest("The checked-out SDK requires its runtime dependencies")

        with tempfile.TemporaryDirectory(prefix="tasksolver-claude-sdk-test-") as directory:
            root: Final = Path(directory)
            executable: Final = root / "claude-stub"
            pid_path: Final = root / "pid"
            received_path: Final = root / "received.json"
            executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
                import json
                import os
                import sys
                from pathlib import Path

                if "--version" in sys.argv:
                    print("2.1.114 (Claude Code)")
                    raise SystemExit(0)

                Path(os.environ["TEST_CLAUDE_PID"]).write_text(str(os.getpid()))

                def emit(message):
                    print(json.dumps(message), flush=True)

                for line in sys.stdin:
                    packet = json.loads(line)
                    if packet["type"] == "control_request":
                        emit({"type": "control_response", "response": {
                            "subtype": "success", "request_id": packet["request_id"],
                            "response": {"commands": [], "models": []},
                        }})
                    elif packet["type"] == "user":
                        Path(os.environ["TEST_CLAUDE_RECEIVED"]).write_text(json.dumps(packet))
                        if packet["message"]["content"] == "hang":
                            continue
                        emit({"type": "assistant", "message": {
                            "role": "assistant", "model": "claude-stub",
                            "content": [{"type": "text", "text": "Intermediate"}],
                        }})
                        emit({"type": "result", "subtype": "success", "is_error": False,
                              "duration_ms": 1, "duration_api_ms": 1, "num_turns": 1,
                              "session_id": "stub-session", "result": "4",
                              "usage": {"input_tokens": 3, "output_tokens": 1},
                              "total_cost_usd": 0})
                '''))
            executable.chmod(0o700)
            kwargs: Final = dict(
                workspace=directory, timeout=10,
                extra_env={"TEST_CLAUDE_PID": str(pid_path),
                           "TEST_CLAUDE_RECEIVED": str(received_path)},
                sdk_options={"cli_path": str(executable)},
            )
            with patch.object(client, "_load_sdk", return_value=claude_agent_sdk):
                response: Final = client.ask("2 + 2", **kwargs)
                self.assertEqual(response.text, "4")
                self.assertEqual(response.session_id, "stub-session")
                self.assertEqual(json.loads(received_path.read_text())["message"]["content"],
                                 "2 + 2")
                completed_pid: Final = int(pid_path.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(completed_pid, 0)
                kwargs["timeout"] = 0.25
                with self.assertRaises(TimeoutError):
                    client.ask("hang", **kwargs)
                timed_out_pid: Final = int(pid_path.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(timed_out_pid, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
