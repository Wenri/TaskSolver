#!/usr/bin/env python3
"""Offline tests for chat mode and the shared image handling; no CLI, credentials or network.

Run with ``python test_scripts/test_chat_mode.py``. Chat mode (``chat=True`` on the Claude and
Codex SDK backends and on agy) must turn an agent runtime into a plain chat call: no MCP servers
or injected context, thinking off or lowest, no tools (Claude, Codex: inline images) or only
agy's view_file on the files it was given, and a hard failure otherwise. The live checks of the
resulting requests are not repeated here.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from collections.abc import Mapping
from typing import Final, Unpack, cast
from unittest.mock import patch

_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT / "claude"), str(_ROOT / "claude" / "vendor" / "sdk" / "src"),
               str(_ROOT / "codex"), str(_ROOT / "codex" / "vendor" / "sdk" / "python" / "src"),
               str(_ROOT / "antigravity"), str(_ROOT)]

from PIL import Image

from pyclaude import client
from pyclaude.types import AskOptions as ClaudeAskOptions, Prompt
from pyclaude.model import CHAT_ENV, ClaudeAgentModel, thinking_config
from pycodex.sdk import CodexSDKResponse, SDKSession
from pycodex.types import SDKAskOptions, SDKSessionOptions
from openai_codex import TurnResult
from openai_codex._inputs import RunInput
from openai_codex.types import ThreadItem, TurnStatus
from wirecap.types import JSONObject
from pyagy.client import AgyResponse
from pycodex.model import CHAT_CONFIG, CodexModel, configured_mcp_servers
from pyagy._env import linked_env
from pyagy.model import CHAT_AGENT, AgyModel
from tasksolver.agent import Agent
from tasksolver.cli_backend import WORKSPACE_IMAGE_DIR, CLIPayload, ChatModeViolation, CLIBackendModel
from tasksolver.common import ParsedAnswer, Question, TaskSpec, image_media_type


class Text(ParsedAnswer):
    def __init__(self, text: str) -> None:
        self.text = text

    @staticmethod
    def parser(gpt_raw: str) -> Text:
        return Text(gpt_raw)

    def __str__(self) -> str:
        return self.text


TASK: Final = TaskSpec(name="t", description="d", answer_type=Text,
                       followup_func=None, completed_func=None)


def image(color: tuple[int, int, int] = (0, 255, 0)) -> Image.Image:
    img = Image.new("RGB", (8, 6), (10, 20, 30))
    img.putpixel((3, 2), color)          # a single-pixel mark, as keypoint judges draw
    return img


def decode(url: str) -> tuple[str, bytes]:
    header, _, data = url.partition(",")
    return header, base64.b64decode(data)


def empty_payload() -> CLIPayload:
    return {"prompt": "", "max_tokens": 1000, "workspace": None}


class OptionsFactory:
    def ClaudeAgentOptions(self, **options: object) -> dict[str, object]:
        return options


def sdk_response(items: list[JSONObject]) -> CodexSDKResponse:
    result = TurnResult(id="turn", status=TurnStatus.completed, error=None,
                        started_at=None, completed_at=None, duration_ms=None,
                        final_response="Score: 1", usage=None,
                        items=[ThreadItem.model_validate(item) for item in items])
    return CodexSDKResponse(text="Score: 1", transcript="", exit_status=0, workspace="w",
                            capture_path="", turns=[], session_id="s", sdk_result=result)


class ImageHandling(unittest.TestCase):
    def test_pil_data_url_is_labelled_png(self) -> None:
        header, data = decode(Question.get_pil_image_content(image())["image_url"]["url"])
        self.assertEqual(header, "data:image/png;base64")
        self.assertEqual(image_media_type(data), "image/png")

    def test_local_image_label_follows_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.jpg")
            image().save(path, format="PNG")      # the extension lies; the bytes decide
            header, _ = decode(Question.get_local_image_content(path)["image_url"]["url"])
            self.assertEqual(header, "data:image/png;base64")

    def test_saved_copy_is_lossless_absolute_and_outside_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"TASKSOLVER_IMAGE_DIR": d}):
            cwd = os.getcwd()
            saved_content = Question.get_pil_image_content_savecopy(image())
            assert "local_path" in saved_content
            path = saved_content["local_path"]
            self.assertTrue(os.path.isabs(path) and path.startswith(d) and path.endswith(".png"))
            self.assertFalse(os.path.exists(os.path.join(cwd, "temporary")))
            with Image.open(path) as saved:
                self.assertEqual(saved.getpixel((3, 2)), (0, 255, 0))   # no JPEG blur


class Payloads(unittest.TestCase):
    def test_legacy_client_callable_subclass_contract(self) -> None:
        def sample(prompt: str | list[JSONObject], n: int,
                   **kwargs: object) -> list[client.ClaudeResponse]:
            prefix = kwargs["prefix"]
            assert isinstance(prefix, str) and isinstance(prompt, str)
            return [client.ClaudeResponse(text=prefix + prompt) for _ in range(n)]

        class LegacyModel(CLIBackendModel[client.ClaudeResponse]):
            _client_ask_many = staticmethod(sample)

            def _call_kwargs(self, payload: CLIPayload) -> dict[str, object]:
                return {"prefix": "Answer: "}

        messages, _metadata = LegacyModel().ask({"prompt": "4"}, n_choices=2)
        self.assertEqual(messages, [{"role": "assistant", "content": "Answer: 4"}] * 2)

    def test_chat_payload_keeps_order_and_writes_no_files(self) -> None:
        q = Question([image(), "between", image((255, 0, 0)), "last"])
        with tempfile.TemporaryDirectory() as d:
            p = CLIBackendModel.prepare_payload(q, workspace=d, inline=True)
            self.assertEqual(os.listdir(d), [])
        assert "content" in p and "image_paths" in p
        self.assertEqual([part["type"] for part in p["content"]], ["image", "text", "image", "text"])
        self.assertTrue(all(part["url"].startswith("data:image/png;base64,")
                            for part in p["content"] if part["type"] == "image"))
        self.assertEqual(p["prompt"], "between\n\nlast")
        self.assertEqual(p["image_paths"], [])

    def test_file_payload_uses_absolute_paths_inside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = CLIBackendModel.prepare_payload(Question([image(), "q"]), workspace=d)
            assert "image_paths" in p
            (path,) = p["image_paths"]
            self.assertEqual(os.path.dirname(path), os.path.join(d, WORKSPACE_IMAGE_DIR))
            self.assertIn(f"Image 1: {path}", p["prompt"])


class ClaudeChat(unittest.TestCase):
    def test_direct_ask_accepts_prompt_only_payload(self) -> None:
        model = ClaudeAgentModel(task=TASK)
        seen: list[Prompt] = []

        def sample(prompt: Prompt, n: int, *, max_workers: int | None = None,
                   **kwargs: Unpack[ClaudeAskOptions]) -> list[client.ClaudeResponse]:
            seen.append(prompt)
            return [client.ClaudeResponse(text="4", result={"subtype": "success"})]

        with patch.object(model, "_client_ask_many", sample):
            messages, _metadata = model.ask({"prompt": "Return 4."})
        self.assertEqual(seen, ["Return 4."])
        self.assertEqual(messages, [{"role": "assistant", "content": "4"}])

    def test_chat_defaults(self) -> None:
        m = ClaudeAgentModel(task=TASK, model="claude-sonnet-4-6", chat=True)
        kw = m._call_kwargs(empty_payload())
        self.assertEqual((kw.get("tools"), kw.get("allowed_tools"), kw.get("mcp_servers")), ([], [], {}))
        opts = kw.get("sdk_options")
        assert opts is not None
        self.assertEqual(opts["thinking"], {"type": "disabled"})
        self.assertEqual(opts["effort"], "low")
        self.assertEqual((opts["setting_sources"], opts["skills"], opts["max_turns"]), ([], [], 1))
        self.assertEqual(opts["extra_args"], {"no-session-persistence": None})
        env = kw.get("extra_env")
        workspace = kw.get("workspace")
        assert env is not None and workspace is not None
        self.assertEqual({k: env[k] for k in CHAT_ENV}, CHAT_ENV)
        self.assertTrue(os.path.isdir(workspace))
        self.assertTrue(os.path.basename(str(workspace)).startswith("tasksolver-claude-chat-"))

    def test_chat_overrides_and_conflicts(self) -> None:
        m = ClaudeAgentModel(task=TASK, chat=True, thinking="adaptive", effort="medium",
                             system_prompt="Judge.", workspace="/tmp")
        opts = m._call_kwargs(empty_payload()).get("sdk_options")
        assert opts is not None
        self.assertEqual((opts["thinking"], opts["effort"], opts["system_prompt"]),
                         ({"type": "adaptive"}, "medium", "Judge."))
        with self.assertRaises(ValueError):
            ClaudeAgentModel(task=TASK, chat=True, mcp_servers={"x": {"command": "x"}})
        with self.assertRaises(ValueError):
            ClaudeAgentModel(task=TASK, chat=True, tools=["Read"])
        self.assertEqual(thinking_config(2048), {"type": "enabled", "budget_tokens": 2048})
        self.assertEqual(thinking_config(False), {"type": "disabled"})

    def test_sdk_options_are_strict_and_leave_the_parent_session(self) -> None:
        m = ClaudeAgentModel(task=TASK, chat=True)
        parent = {"CLAUDE_CODE_SESSION_ID": "parent", "CLAUDE_CODE_MESSAGING_SOCKET": "/s",
                  "CLAUDE_EFFORT": "max", "CLAUDE_PID": "1"}
        kw = m._call_kwargs(empty_payload())
        with patch.dict(os.environ, parent):
            opts = client._options(
                OptionsFactory(), session_id=None, model=kw.get("model"),
                workspace=kw.get("workspace"), api_key=kw.get("api_key"),
                extra_env=kw.get("extra_env"), mcp_servers=kw.get("mcp_servers"),
                tools=kw.get("tools"), allowed_tools=kw.get("allowed_tools"),
                permission_mode=kw.get("permission_mode", "default"),
                sdk_options=kw.get("sdk_options"),
            )
        self.assertTrue(opts["strict_mcp_config"])
        self.assertEqual((opts["mcp_servers"], opts["tools"], opts["allowed_tools"]), ({}, [], []))
        env = opts["env"]
        assert isinstance(env, Mapping)
        mapped_env = cast(Mapping[object, object], env)
        self.assertEqual({k: mapped_env[k] for k in parent}, dict.fromkeys(parent, ""))

    def test_tool_use_fails_a_chat_turn(self) -> None:
        m = ClaudeAgentModel(task=TASK, chat=True)
        reply = client.ClaudeResponse(text="Score: 1",
            messages=[{"type": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {}}]}])
        with self.assertRaises(ChatModeViolation):
            m._finish(reply)
        reply.messages = [{"type": "assistant", "content": [{"type": "text", "text": "Score: 1"}]}]
        m._finish(reply)


class CodexChat(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        with open(os.path.join(self.home.name, "config.toml"), "w") as f:
            f.write('model = "x"\n[mcp_servers.node_repl]\ncommand = "node_repl"\n'
                    '[mcp_servers.docs]\ncommand = "docs"\n')

    def tearDown(self) -> None:
        self.home.cleanup()

    def test_chat_call_kwargs(self) -> None:
        m = CodexModel(task=TASK, model="gpt-5.5", chat=True, codex_home=self.home.name)
        kw = m._call_kwargs(empty_payload())
        thread = kw.get("thread_options")
        assert thread is not None
        self.assertTrue(thread.get("ephemeral"))
        self.assertEqual(thread.get("base_instructions"), "")
        config = thread.get("config")
        assert config is not None
        for key, value in CHAT_CONFIG.items():
            self.assertEqual(config[key], value)
        self.assertFalse(config["mcp_servers.node_repl.enabled"])
        self.assertFalse(config["mcp_servers.docs.enabled"])
        self.assertEqual(config["model_reasoning_effort"], "none")
        self.assertEqual(kw.get("turn_options"), {"effort": "none"})
        extra_env = kw.get("extra_env")
        assert extra_env is not None
        self.assertEqual(extra_env["CODEX_EXEC_SERVER_URL"], "none")
        self.assertIs(kw.get("capture"), False)

    def test_chat_options_override(self) -> None:
        m = CodexModel(task=TASK, chat=True, codex_home=self.home.name, effort="low",
                       system_prompt="Judge.", sdk_options={"capture": True, "thread_options": {
                           "config": {"web_search": "cached"}}})
        kw = m._call_kwargs(empty_payload())
        thread = kw.get("thread_options")
        assert thread is not None
        self.assertEqual(thread.get("base_instructions"), "Judge.")
        config = thread.get("config")
        assert config is not None
        self.assertEqual(config["web_search"], "cached")
        self.assertEqual(kw.get("turn_options"), {"effort": "low"})
        self.assertIs(kw.get("capture"), True)
        with self.assertRaises(ValueError):
            CodexModel(task=TASK, chat=True, transport="exec")
        with self.assertRaises(ValueError):
            CodexModel(task=TASK, chat=True, mcp_servers={"x": {"command": "x"}})
        self.assertEqual(configured_mcp_servers(os.path.join(self.home.name, "missing")), [])

    def test_chat_inputs_keep_question_order(self) -> None:
        from openai_codex import ImageInput, TextInput
        seen: list[RunInput] = []

        def fake_ask_many(prompt: RunInput, n: int, **kwargs: Unpack[SDKAskOptions]) -> list[CodexSDKResponse]:
            seen.append(prompt)
            return [sdk_response([{"type": "agentMessage", "id": "a", "text": "Score: 1"}])]

        m = CodexModel(task=TASK, chat=True, codex_home=self.home.name)
        m._client_ask_many = fake_ask_many
        parsed, _raw, _meta, _payload = m.rough_guess(Question([image(), image(), "prompt"]))
        self.assertEqual(str(parsed), "Score: 1")
        prompt = seen[0]
        assert isinstance(prompt, list)
        self.assertEqual([type(part) for part in prompt], [ImageInput, ImageInput, TextInput])
        first = prompt[0]
        assert isinstance(first, ImageInput)
        self.assertTrue(first.url.startswith("data:image/png;base64,"))

    def test_agent_actions_fail_a_chat_turn(self) -> None:
        m = CodexModel(task=TASK, chat=True, codex_home=self.home.name)
        reply = sdk_response([
            {"type": "userMessage", "id": "u", "content": []},
            {"type": "reasoning", "id": "r"},
            {"type": "commandExecution", "id": "c", "command": "true", "cwd": "/tmp",
             "commandActions": [], "status": "completed"},
            {"type": "agentMessage", "id": "a", "text": "x"},
        ])
        with self.assertRaises(ChatModeViolation):
            m._finish(reply)

    def test_prompt_only_payload_reaches_chat_sdk(self) -> None:
        seen: list[RunInput] = []

        def sample(prompt: RunInput, n: int, **kwargs: Unpack[SDKAskOptions]) -> list[CodexSDKResponse]:
            seen.append(prompt)
            return [sdk_response([{"type": "agentMessage", "id": "a", "text": "Score: 1"}])]

        model = CodexModel(task=TASK, chat=True, codex_home=self.home.name)
        model._client_ask_many = sample
        messages, _ = model.ask({"prompt": "Score this answer"})
        from openai_codex import TextInput
        self.assertEqual(seen, [[TextInput(text="Score this answer")]])
        self.assertEqual(messages[0]["content"], "Score: 1")

    def test_chat_session_inherits_call_configuration(self) -> None:
        seen: list[SDKSessionOptions] = []

        def session(**kwargs: Unpack[SDKSessionOptions]) -> SDKSession:
            seen.append(kwargs)
            return SDKSession.__new__(SDKSession)

        model = CodexModel(task=TASK, chat=True, codex_home=self.home.name,
                           system_prompt="Grade the answer")
        with patch("pycodex.model.SDKSession", side_effect=session):
            model.sdk_session()
        options = seen[0]
        thread = options.get("thread_options")
        assert thread is not None
        self.assertTrue(thread.get("ephemeral"))
        self.assertEqual(thread.get("base_instructions"), "Grade the answer")
        config = thread.get("config")
        assert config is not None
        self.assertFalse(config["features.shell_tool"])
        self.assertEqual(config["model_reasoning_effort"], "none")
        env = options.get("extra_env")
        assert env is not None
        self.assertEqual(env.get("CODEX_EXEC_SERVER_URL"), "none")
        self.assertFalse(options.get("capture"))


class AgyChat(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = tempfile.TemporaryDirectory()
        self.model = AgyModel(task=TASK, model="gemini-3.1-pro-low", chat=True,
                              workspace=self.ws.name)

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_agent_definition_and_call_kwargs(self) -> None:
        md = Path(self.ws.name, ".agents", "agents", CHAT_AGENT, "agent.md").read_text()
        for line in ("tools:\n  - view_file\n", "excludeDefaultComponents: true",
                     "inheritCustomizations: false", "commandExecutionPolicy: off"):
            self.assertIn(line, md)
        kw = self.model._call_kwargs(empty_payload())
        self.assertEqual(kw.get("extra_flags"), ["--agent", CHAT_AGENT, "--effort", "low"])
        env = kw.get("extra_env")
        assert env is not None
        self.assertEqual({k: env[k] for k in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY")},
                         dict.fromkeys(("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY")))
        self.assertIs(kw.get("trust"), False)
        with self.assertRaises(ValueError):
            AgyModel(task=TASK, chat=True, workspace=self.ws.name, mcp_servers={"x": {"command": "x"}})

    def test_images_stay_files_named_for_view_file(self) -> None:
        p = self.model.prepare_payload(Question([image(), "q"]), workspace=self.ws.name,
                                       inline=self.model._inline_images())
        assert "image_paths" in p
        (path,) = p["image_paths"]
        self.assertTrue(os.path.isfile(path))
        self.assertIn("view_file", p["prompt"])

    def test_only_view_file_on_given_images(self) -> None:
        allowed = os.path.join(self.ws.name, "a.png")
        self.model._chat_paths = {os.path.realpath(allowed)}

        def reply(*calls: tuple[str, JSONObject]) -> AgyResponse:
            parts: list[JSONObject] = [{"functionCall": {"name": n, "args": a}} for n, a in calls]
            return AgyResponse(text="", transcript="", exit_status=0, capture_path=None,
                workspace=self.ws.name, linked=True,
                turns=[{"events": [{"response": {"candidates": [{"content": {"parts": parts}}]}}]}])

        self.assertEqual(self.model._chat_violations(reply(("view_file", {"AbsolutePath": allowed}))), [])
        self.assertEqual(self.model._chat_violations(reply(("run_command", {"CommandLine": "ls"}))),
                         ["run_command"])
        self.assertEqual(self.model._chat_violations(reply(("view_file", {"AbsolutePath": "/etc/hosts"}))),
                         ["view_file(/etc/hosts)"])

    def test_extra_env_none_unsets(self) -> None:
        with patch.dict(os.environ, {"SSH_CLIENT": "1.2.3.4 5 22"}):
            env = linked_env(capture=os.path.join(self.ws.name, "c.jsonl"),
                                   extra_env={"SSH_CLIENT": None, "X_KEEP": "1"})
        self.assertNotIn("SSH_CLIENT", env)
        self.assertEqual(env["X_KEEP"], "1")


class VendoredArtifacts(unittest.TestCase):
    def test_sibling_artifact_through_a_symlinked_package(self) -> None:
        from wirecap.runtime.vendor import vendored
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "checkout")
            os.makedirs(os.path.join(real, "pkg"))
            os.makedirs(os.path.join(real, "vendor"))
            open(os.path.join(real, "vendor", "tool"), "w").close()
            shim = os.path.join(d, "shim")
            os.makedirs(shim)
            os.symlink(os.path.join(real, "pkg"), os.path.join(shim, "pkg"))
            found = vendored(os.path.join(shim, "pkg"), "pkg", "vendor/tool", "../vendor/tool")
            # callers abspath() the result; that must still name the artifact
            self.assertTrue(os.path.isfile(os.path.abspath(found)))


class AgentChat(unittest.TestCase):
    def test_chat_reaches_the_sdk_backends(self) -> None:
        claude = Agent(None, TASK, vision_model="claude-code-sonnet-4-6", chat=True,
                       backend_options={"effort": "medium"}).visual_interface
        assert isinstance(claude, ClaudeAgentModel)
        self.assertTrue(claude.chat)
        options = claude._call_kwargs().get("sdk_options")
        assert options is not None
        self.assertEqual(options["effort"], "medium")
        codex = Agent(None, TASK, vision_model="codex-gpt-5.5", chat=True,
                      backend_options={"timeout": 120}).visual_interface
        assert isinstance(codex, CodexModel)
        self.assertTrue(codex.chat)
        self.assertEqual(codex.timeout, 120)

    def test_kimi_code_refuses_chat_mode(self) -> None:
        with self.assertRaises(ValueError):
            Agent(None, TASK, vision_model="kimi-code-k3", chat=True)

    def test_chat_reaches_agy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            agy = Agent(None, TASK, vision_model="agy-gemini-3.1-pro-low", chat=True,
                        workspace=d).visual_interface
            assert isinstance(agy, AgyModel)
            self.assertTrue(agy.chat)
            self.assertEqual(agy.model, "gemini-3.1-pro-low")

    def test_backend_options_need_a_cli_backend(self) -> None:
        with self.assertRaises(ValueError):
            Agent("key", TASK, vision_model="gpt-4o", backend_options={"effort": "low"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
