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
from types import SimpleNamespace
from typing import Final
from unittest.mock import patch

_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT / "claude"), str(_ROOT / "claude" / "vendor" / "sdk" / "src"),
               str(_ROOT / "codex"), str(_ROOT / "codex" / "vendor" / "sdk" / "python" / "src"),
               str(_ROOT / "antigravity"), str(_ROOT)]

from PIL import Image

from pyclaude import client
from pyclaude.model import CHAT_ENV, ClaudeAgentModel, thinking_config
from pycodex.model import CHAT_CONFIG, CodexModel, configured_mcp_servers
from pyagy._env import instrumented_env
from pyagy.model import CHAT_AGENT, AgyModel
from tasksolver.agent import Agent
from tasksolver.cli_backend import WORKSPACE_IMAGE_DIR, ChatModeViolation, CLIBackendModel
from tasksolver.common import ParsedAnswer, Question, TaskSpec, image_media_type


class Text(ParsedAnswer):
    def __init__(self, text):
        self.text = text

    @staticmethod
    def parser(raw):
        return Text(raw)

    def __str__(self):
        return self.text


TASK: Final = TaskSpec(name="t", description="d", answer_type=Text,
                       followup_func=None, completed_func=None)


def image(color=(0, 255, 0)):
    img = Image.new("RGB", (8, 6), (10, 20, 30))
    img.putpixel((3, 2), color)          # a single-pixel mark, as keypoint judges draw
    return img


def decode(url):
    header, _, data = url.partition(",")
    return header, base64.b64decode(data)


class ImageHandling(unittest.TestCase):
    def test_pil_data_url_is_labelled_png(self):
        header, data = decode(Question.get_pil_image_content(image())["image_url"]["url"])
        self.assertEqual(header, "data:image/png;base64")
        self.assertEqual(image_media_type(data), "image/png")

    def test_local_image_label_follows_file_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.jpg")
            image().save(path, format="PNG")      # the extension lies; the bytes decide
            header, _ = decode(Question.get_local_image_content(path)["image_url"]["url"])
            self.assertEqual(header, "data:image/png;base64")

    def test_saved_copy_is_lossless_absolute_and_outside_cwd(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"TASKSOLVER_IMAGE_DIR": d}):
            cwd = os.getcwd()
            path = Question.get_pil_image_content_savecopy(image())["local_path"]
            self.assertTrue(os.path.isabs(path) and path.startswith(d) and path.endswith(".png"))
            self.assertFalse(os.path.exists(os.path.join(cwd, "temporary")))
            with Image.open(path) as saved:
                self.assertEqual(saved.getpixel((3, 2)), (0, 255, 0))   # no JPEG blur


class Payloads(unittest.TestCase):
    def test_chat_payload_keeps_order_and_writes_no_files(self):
        q = Question([image(), "between", image((255, 0, 0)), "last"])
        with tempfile.TemporaryDirectory() as d:
            p = CLIBackendModel.prepare_payload(q, workspace=d, inline=True)
            self.assertEqual(os.listdir(d), [])
        self.assertEqual([part["type"] for part in p["content"]], ["image", "text", "image", "text"])
        self.assertTrue(all(part["url"].startswith("data:image/png;base64,")
                            for part in p["content"] if part["type"] == "image"))
        self.assertEqual(p["prompt"], "between\n\nlast")
        self.assertEqual(p["image_paths"], [])

    def test_file_payload_uses_absolute_paths_inside_the_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            p = CLIBackendModel.prepare_payload(Question([image(), "q"]), workspace=d)
            (path,) = p["image_paths"]
            self.assertEqual(os.path.dirname(path), os.path.join(d, WORKSPACE_IMAGE_DIR))
            self.assertIn(f"Image 1: {path}", p["prompt"])


class ClaudeChat(unittest.TestCase):
    def test_chat_defaults(self):
        m = ClaudeAgentModel(task=TASK, model="claude-sonnet-4-6", chat=True)
        kw = m._call_kwargs({})
        self.assertEqual((kw["tools"], kw["allowed_tools"], kw["mcp_servers"]), ([], [], {}))
        opts = kw["sdk_options"]
        self.assertEqual(opts["thinking"], {"type": "disabled"})
        self.assertEqual(opts["effort"], "low")
        self.assertEqual((opts["setting_sources"], opts["skills"], opts["max_turns"]), ([], [], 1))
        self.assertIn("no-session-persistence", opts["extra_args"])
        self.assertEqual({k: kw["extra_env"][k] for k in CHAT_ENV}, CHAT_ENV)
        self.assertTrue(os.path.isdir(kw["workspace"]))
        self.assertTrue(os.path.basename(kw["workspace"]).startswith("tasksolver-claude-chat-"))

    def test_chat_overrides_and_conflicts(self):
        m = ClaudeAgentModel(task=TASK, chat=True, thinking="adaptive", effort="medium",
                             system_prompt="Judge.", workspace="/tmp")
        opts = m._call_kwargs({})["sdk_options"]
        self.assertEqual((opts["thinking"], opts["effort"], opts["system_prompt"]),
                         ({"type": "adaptive"}, "medium", "Judge."))
        with self.assertRaises(ValueError):
            ClaudeAgentModel(task=TASK, chat=True, mcp_servers={"x": {"command": "x"}})
        with self.assertRaises(ValueError):
            ClaudeAgentModel(task=TASK, chat=True, tools=["Read"])
        self.assertEqual(thinking_config(2048), {"type": "enabled", "budget_tokens": 2048})
        self.assertEqual(thinking_config(False), {"type": "disabled"})

    def test_sdk_options_are_strict_and_leave_the_parent_session(self):
        m = ClaudeAgentModel(task=TASK, chat=True)
        parent = {"CLAUDE_CODE_SESSION_ID": "parent", "CLAUDE_CODE_MESSAGING_SOCKET": "/s",
                  "CLAUDE_EFFORT": "max", "CLAUDE_PID": "1"}
        with patch.dict(os.environ, parent):
            opts = client._options(SimpleNamespace(ClaudeAgentOptions=lambda **kw: kw),
                                   session_id=None, **{k: v for k, v in m._call_kwargs({}).items()
                                                       if k != "timeout"})
        self.assertTrue(opts["strict_mcp_config"])
        self.assertEqual((opts["mcp_servers"], opts["tools"], opts["allowed_tools"]), ({}, [], []))
        self.assertEqual({k: opts["env"][k] for k in parent}, dict.fromkeys(parent, ""))

    def test_tool_use_fails_a_chat_turn(self):
        m = ClaudeAgentModel(task=TASK, chat=True)
        reply = SimpleNamespace(text="Score: 1", transcript="", exit_status=0, workspace=None,
                                messages=[{"type": "assistant", "content": [
                                    {"type": "tool_use", "name": "Read", "input": {}}]}],
                                metadata=lambda: {})
        with self.assertRaises(ChatModeViolation):
            m._finish(reply)
        reply.messages = [{"type": "assistant", "content": [{"type": "text", "text": "Score: 1"}]}]
        m._finish(reply)


class CodexChat(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        with open(os.path.join(self.home.name, "config.toml"), "w") as f:
            f.write('model = "x"\n[mcp_servers.node_repl]\ncommand = "node_repl"\n'
                    '[mcp_servers.docs]\ncommand = "docs"\n')

    def tearDown(self):
        self.home.cleanup()

    def test_chat_call_kwargs(self):
        m = CodexModel(task=TASK, model="gpt-5.5", chat=True, codex_home=self.home.name)
        kw = m._call_kwargs({"workspace": None})
        thread = kw["thread_options"]
        self.assertTrue(thread["ephemeral"])
        self.assertEqual(thread["base_instructions"], "")
        config = thread["config"]
        for key, value in CHAT_CONFIG.items():
            self.assertEqual(config[key], value)
        self.assertFalse(config["mcp_servers.node_repl.enabled"])
        self.assertFalse(config["mcp_servers.docs.enabled"])
        self.assertEqual(config["model_reasoning_effort"], "none")
        self.assertEqual(kw["turn_options"], {"effort": "none"})
        self.assertEqual(kw["extra_env"]["CODEX_EXEC_SERVER_URL"], "none")
        self.assertIs(kw["capture"], False)

    def test_chat_options_override(self):
        m = CodexModel(task=TASK, chat=True, codex_home=self.home.name, effort="low",
                       system_prompt="Judge.", sdk_options={"capture": True, "thread_options": {
                           "config": {"web_search": "cached"}}})
        kw = m._call_kwargs({})
        self.assertEqual(kw["thread_options"]["base_instructions"], "Judge.")
        self.assertEqual(kw["thread_options"]["config"]["web_search"], "cached")
        self.assertEqual(kw["turn_options"], {"effort": "low"})
        self.assertIs(kw["capture"], True)
        with self.assertRaises(ValueError):
            CodexModel(task=TASK, chat=True, transport="exec")
        with self.assertRaises(ValueError):
            CodexModel(task=TASK, chat=True, mcp_servers={"x": {"command": "x"}})
        self.assertEqual(configured_mcp_servers(os.path.join(self.home.name, "missing")), [])

    def test_chat_inputs_keep_question_order(self):
        from openai_codex import ImageInput, TextInput
        seen = {}

        def fake_ask_many(prompt, n, **kwargs):
            seen["prompt"], seen["kwargs"] = prompt, kwargs
            item = SimpleNamespace(root=SimpleNamespace(type="agentMessage"))
            return [SimpleNamespace(text="Score: 1", transcript="", exit_status=0, workspace="w",
                                    model="gpt-5.5", usage={}, timed_out=False, session_id="s",
                                    capture_path="", turns=[], explicit_reasoning_output="",
                                    sdk_result=SimpleNamespace(items=[item]))]

        m = CodexModel(task=TASK, chat=True, codex_home=self.home.name)
        m._client_ask_many = fake_ask_many
        parsed, raw, meta, payload = m.rough_guess(Question([image(), image(), "prompt"]))
        self.assertEqual(str(parsed), "Score: 1")
        kinds = [type(p) for p in seen["prompt"]]
        self.assertEqual(kinds, [ImageInput, ImageInput, TextInput])
        self.assertTrue(seen["prompt"][0].url.startswith("data:image/png;base64,"))

    def test_agent_actions_fail_a_chat_turn(self):
        m = CodexModel(task=TASK, chat=True, codex_home=self.home.name)
        items = [SimpleNamespace(root=SimpleNamespace(type=t))
                 for t in ("userMessage", "reasoning", "commandExecution", "agentMessage")]
        reply = SimpleNamespace(text="x", transcript="", exit_status=0, workspace="w", model="m",
                                usage={}, timed_out=False, session_id="s", capture_path="",
                                turns=[], explicit_reasoning_output="",
                                sdk_result=SimpleNamespace(items=items))
        with self.assertRaises(ChatModeViolation):
            m._finish(reply)


class AgyChat(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.TemporaryDirectory()
        self.model = AgyModel(task=TASK, model="gemini-3.1-pro-low", chat=True,
                              workspace=self.ws.name)

    def tearDown(self):
        self.ws.cleanup()

    def test_agent_definition_and_call_kwargs(self):
        md = Path(self.ws.name, ".agents", "agents", CHAT_AGENT, "agent.md").read_text()
        for line in ("tools:\n  - view_file\n", "excludeDefaultComponents: true",
                     "inheritCustomizations: false", "commandExecutionPolicy: off"):
            self.assertIn(line, md)
        kw = self.model._call_kwargs({"workspace": None})
        self.assertEqual(kw["extra_flags"], ["--agent", CHAT_AGENT, "--effort", "low"])
        self.assertEqual({k: kw["extra_env"][k] for k in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY")},
                         dict.fromkeys(("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY")))
        self.assertIs(kw["trust"], False)
        with self.assertRaises(ValueError):
            AgyModel(task=TASK, chat=True, workspace=self.ws.name, mcp_servers={"x": {"command": "x"}})

    def test_images_stay_files_named_for_view_file(self):
        p = self.model.prepare_payload(Question([image(), "q"]), workspace=self.ws.name,
                                       inline=self.model._inline_images())
        (path,) = p["image_paths"]
        self.assertTrue(os.path.isfile(path))
        self.assertIn("view_file", p["prompt"])

    def test_only_view_file_on_given_images(self):
        allowed = os.path.join(self.ws.name, "a.png")
        self.model._chat_paths = {os.path.realpath(allowed)}

        def reply(*calls):
            parts = [{"functionCall": {"name": n, "args": a}} for n, a in calls]
            return SimpleNamespace(turns=[{"events": [{"response": {"candidates": [
                {"content": {"parts": parts}}]}}]}])

        self.assertEqual(self.model._chat_violations(reply(("view_file", {"AbsolutePath": allowed}))), [])
        self.assertEqual(self.model._chat_violations(reply(("run_command", {"CommandLine": "ls"}))),
                         ["run_command"])
        self.assertEqual(self.model._chat_violations(reply(("view_file", {"AbsolutePath": "/etc/hosts"}))),
                         ["view_file(/etc/hosts)"])

    def test_extra_env_none_unsets(self):
        with patch.dict(os.environ, {"SSH_CLIENT": "1.2.3.4 5 22"}):
            env = instrumented_env(capture=os.path.join(self.ws.name, "c.jsonl"),
                                   extra_env={"SSH_CLIENT": None, "X_KEEP": "1"})
        self.assertNotIn("SSH_CLIENT", env)
        self.assertEqual(env["X_KEEP"], "1")


class VendoredArtifacts(unittest.TestCase):
    def test_sibling_artifact_through_a_symlinked_package(self):
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
    def test_chat_reaches_the_sdk_backends(self):
        claude = Agent(None, TASK, vision_model="claude-code-sonnet-4-6", chat=True,
                       backend_options={"effort": "medium"}).visual_interface
        self.assertTrue(claude.chat)
        self.assertEqual(claude._call_kwargs({})["sdk_options"]["effort"], "medium")
        codex = Agent(None, TASK, vision_model="codex-gpt-5.5", chat=True,
                      backend_options={"timeout": 120}).visual_interface
        self.assertTrue(codex.chat)
        self.assertEqual(codex.timeout, 120)

    def test_kimi_code_refuses_chat_mode(self):
        with self.assertRaises(ValueError):
            Agent(None, TASK, vision_model="kimi-code-k3", chat=True)

    def test_chat_reaches_agy(self):
        with tempfile.TemporaryDirectory() as d:
            agy = Agent(None, TASK, vision_model="agy-gemini-3.1-pro-low", chat=True,
                        workspace=d).visual_interface
            self.assertTrue(agy.chat)
            self.assertEqual(agy.model, "gemini-3.1-pro-low")

    def test_backend_options_need_a_cli_backend(self):
        with self.assertRaises(ValueError):
            Agent("key", TASK, vision_model="gpt-4o", backend_options={"effort": "low"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
