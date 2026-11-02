#!/usr/bin/env python3
"""Real Claude UI and SDK bridge tests against a loopback-only model fixture."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pyclaude import attach_terminal
from pyclaude.types import SDKResultMessage, TerminalSDKClient
from tasksolver.terminal import TerminalManager, open_terminal
from wirecap.types import JSONObject, json_array, json_loads_object, json_object


class LocalAPI:
    def __init__(self, marker: Path) -> None:
        self.marker = marker
        self.permission_requested = threading.Event()
        self.requests: list[str] = []
        self.started = {name: threading.Event() for name in ("SDK_DELAYED", "HUMAN_DELAYED")}
        self.release = {name: threading.Event() for name in self.started}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                owner.respond(self)

            def do_GET(self) -> None:
                self.send_response(404)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        for gate in self.release.values():
            gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        request = json_loads_object(handler.rfile.read(int(handler.headers.get("Content-Length", "0"))))
        if "/count_tokens" in handler.path:
            self.send(handler, b'{"input_tokens":8}', "application/json")
            return
        messages = json_array(request.get("messages", []))
        text = ""
        tool_finished = False
        for raw_message in messages:
            current = json_object(raw_message)
            if current.get("role") != "user":
                continue
            content = current.get("content", "")
            if isinstance(content, str):
                current_text = content
            else:
                parts: list[str] = []
                for item in json_array(content):
                    block = json_object(item)
                    if block.get("type") == "tool_result" and block.get("tool_use_id") == "tool_local_write":
                        tool_finished = True
                    value = block.get("text")
                    if isinstance(value, str):
                        parts.append(value)
                current_text = "\n".join(parts)
            for marker_match in re.finditer(r"SDK_DELAYED|SDK_PERMISSION|HUMAN_DELAYED|HUMAN_OVERLAP|LOCAL_BRIDGE_OK", current_text):
                text = marker_match.group(0)
        self.requests.append(text)
        selected = next((name for name in self.started if name in text), None)
        if selected is not None:
            self.started[selected].set()
            if not self.release[selected].wait(timeout=20):
                return
        answer = "SDK_ONLY" if "SDK_DELAYED" in text else "HUMAN_ONLY" if "HUMAN_" in text else "LOCAL_BRIDGE_OK"
        tool_input: JSONObject | None = None
        if text == "SDK_PERMISSION":
            if tool_finished:
                answer = "PERMISSION_DONE"
            else:
                self.permission_requested.set()
                tool_input = {"command": "printf approved > " + shlex.quote(str(self.marker)),
                              "description": "Write the temporary offline test marker"}
        message: JSONObject = {
            "id": "msg_local_fixture", "type": "message", "role": "assistant",
            "model": request.get("model", "claude-sonnet-4-6"),
            "content": [{"type": "text", "text": answer}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 8, "output_tokens": 5},
        }
        if tool_input is not None:
            message["content"] = [{"type": "tool_use", "id": "tool_local_write", "name": "Bash", "input": tool_input}]
            message["stop_reason"] = "tool_use"
        if not request.get("stream"):
            self.send(handler, json.dumps(message).encode(), "application/json")
            return
        events: list[JSONObject] = [
            {"type": "message_start", "message": {**message, "content": [], "stop_reason": None,
                                                  "usage": {"input_tokens": 8, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": answer}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        if tool_input is not None:
            events[1] = {"type": "content_block_start", "index": 0,
                         "content_block": {"type": "tool_use", "id": "tool_local_write", "name": "Bash", "input": {}}}
            events[2] = {"type": "content_block_delta", "index": 0,
                         "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input)}}
            events[4] = {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                         "usage": {"output_tokens": 5}}
        data = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
        self.send(handler, data, "text/event-stream")

    @staticmethod
    def send(handler: BaseHTTPRequestHandler, data: bytes, content_type: str) -> None:
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class NativeBridgeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("tmux") is None:
            raise unittest.SkipTest("Native UI checks require tmux on PATH")

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tasksolver-claude-native-session-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        workspace = self.directory / "workspace"
        workspace.mkdir()
        config = self.directory / "config"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({
            "permissions": {"defaultMode": "default", "disableAutoMode": "disable"},
        }))
        self.api = LocalAPI(workspace / "permission-marker")
        self.addCleanup(self.api.close)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CLAUDE", "ANTHROPIC", "AWS", "GOOGLE", "VERTEX", "BEDROCK", "AZURE", "WIRE",
                                      "TASKSOLVER_CLAUDE_"))}
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_API_KEY="tasksolver-local-fixture-key",
                   ANTHROPIC_BASE_URL=self.api.url, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_AUTOUPDATER="1")
        self.manager = TerminalManager(workspace=workspace, env=env, width=120, height=45)
        self.addCleanup(self.manager.close)
        self.screen = open_terminal("claude", manager=self.manager, name="native-claude",
                                    model="sonnet")
        assert self.screen.control_socket is not None
        self.socket = self.screen.control_socket
        deadline = time.monotonic() + 20
        last_action = 0.0
        while time.monotonic() < deadline:
            if self.socket.exists():
                break
            snapshot = self.screen.snapshot()
            self.assertTrue(self.screen.alive, snapshot)
            if time.monotonic() - last_action >= 0.5:
                if "Choose the text style" in snapshot or "Choose the theme" in snapshot:
                    self.screen.send_keys("Enter")
                elif "Do you want to use this API key" in snapshot:
                    self.screen.send_keys("Enter" if "❯ Yes" in snapshot else "Up")
                elif "Security notes" in snapshot or "Safety check" in snapshot:
                    self.screen.send_keys("Enter")
                elif "trust" in snapshot.lower() and ("folder" in snapshot.lower() or "workspace" in snapshot.lower()):
                    self.screen.send_keys("Enter" if "❯ Yes, I trust" in snapshot else "Down")
                last_action = time.monotonic()
            await asyncio.sleep(0.1)
        self.assertTrue(self.socket.exists(), self.screen.snapshot(history=100))
        self.assertTrue(self.screen.alive)

    async def wait_event(self, event: threading.Event) -> None:
        try:
            async with asyncio.timeout(10):
                while not event.is_set():
                    await asyncio.sleep(0.02)
        except TimeoutError:
            self.fail(f"Local API did not receive the turn: {self.api.requests!r}\n"
                      + self.screen.snapshot(history=100))

    async def wait_screen(self, text: str) -> None:
        try:
            async with asyncio.timeout(10):
                while text not in self.screen.snapshot(history=100):
                    await asyncio.sleep(0.05)
        except TimeoutError:
            self.fail(f"Native UI did not show {text!r}; requests={self.api.requests!r}\n"
                      + self.screen.snapshot(history=100))

    async def wait_answer(self, text: str) -> None:
        async with asyncio.timeout(10):
            while True:
                snapshot = self.screen.snapshot()
                if "● " + text in snapshot and "esc to interrupt" not in snapshot.lower():
                    return
                await asyncio.sleep(0.05)

    async def result(self, client: TerminalSDKClient) -> SDKResultMessage:
        async with asyncio.timeout(15):
            async for message in client.receive_response():
                if isinstance(message, SDKResultMessage):
                    return message
        self.fail("SDK stream ended without a native result")

    async def test_sdk_and_human_turns_share_one_ui_without_result_cross_talk(self) -> None:
        async with attach_terminal(self.screen, connect_timeout=5) as client:
            info = await client.get_server_info()
            assert info is not None
            pane_pid = int(self.manager._run(["display-message", "-p", "-t", self.screen.pane_id,
                                              "#{pane_pid}"]).stdout)
            self.assertEqual(info["pid"], pane_pid)
            self.assertEqual(json_object(info["tasksolver_local_bridge"])["permission_owner"], "native_terminal")
            self.assertEqual(info["commands"], [])
            await client.query("SDK_DELAYED: Reply with SDK_ONLY. Do not use tools.")
            await self.wait_event(self.api.started["SDK_DELAYED"])
            self.screen.submit("HUMAN_OVERLAP: Reply with HUMAN_ONLY. Do not use tools.")
            self.api.release["SDK_DELAYED"].set()
            result = await self.result(client)
            self.assertEqual(result.result, "SDK_ONLY")
            self.assertEqual(result.session_id, info["session_id"])
            self.assertFalse(result.is_error)
            assert result.usage is not None
            self.assertEqual(result.usage["input_tokens"], 8)
            self.assertEqual(result.usage["output_tokens"], 5)
            assert result.total_cost_usd is not None
            self.assertGreater(result.total_cost_usd, 0)
            await self.wait_answer("HUMAN_ONLY")
            await client.query("Reply with LOCAL_BRIDGE_OK. Do not use tools.")
            self.assertEqual((await self.result(client)).result, "LOCAL_BRIDGE_OK")
        self.assertTrue(self.screen.alive)
        self.assertIn("SDK_ONLY", self.screen.snapshot(history=100))

    async def test_human_turn_rejects_sdk_query_and_unowned_interrupt(self) -> None:
        async with attach_terminal(self.screen, connect_timeout=5) as client:
            self.screen.submit("HUMAN_DELAYED: Reply with HUMAN_ONLY. Do not use tools.")
            await self.wait_event(self.api.started["HUMAN_DELAYED"])
            with self.assertRaisesRegex(Exception, "does not own"):
                await client.interrupt()
            await client.query("This SDK request must be rejected while the human turn runs.")
            with self.assertRaisesRegex(Exception, "active turn"):
                await self.result(client)
        self.api.release["HUMAN_DELAYED"].set()
        await self.wait_answer("HUMAN_ONLY")
        self.assertTrue(self.screen.alive)

    async def test_disconnecting_during_sdk_turn_keeps_native_ui_and_busy_reservation(self) -> None:
        async with attach_terminal(self.screen, connect_timeout=5) as client:
            info = await client.get_server_info()
            await client.query("SDK_DELAYED: Reply with SDK_ONLY. Do not use tools.")
            await self.wait_event(self.api.started["SDK_DELAYED"])
        self.assertTrue(self.screen.alive)
        async with attach_terminal(self.screen, connect_timeout=5) as other:
            other_info = await other.get_server_info()
            assert info is not None and other_info is not None
            self.assertEqual((other_info["pid"], other_info["session_id"]),
                             (info["pid"], info["session_id"]))
            await other.query("Do not take over the disconnected controller's turn.")
            with self.assertRaisesRegex(Exception, "pending SDK query"):
                await self.result(other)
        self.api.release["SDK_DELAYED"].set()
        await self.wait_answer("SDK_ONLY")
        async with attach_terminal(self.screen, connect_timeout=5) as reconnected:
            await reconnected.query("Reply with LOCAL_BRIDGE_OK. Do not use tools.")
            self.assertEqual((await self.result(reconnected)).result, "LOCAL_BRIDGE_OK")
        self.assertTrue(self.screen.alive)

    async def test_native_permission_dialog_owns_tool_approval(self) -> None:
        async with attach_terminal(self.screen, connect_timeout=5) as client:
            await client.query("SDK_PERMISSION: use Bash to write the offline marker, then report completion.")
            result_task = asyncio.create_task(self.result(client))
            try:
                await self.wait_event(self.api.permission_requested)
                await self.wait_screen("Do you want to proceed?")
                self.assertIn("permission-marker", self.screen.snapshot())
                self.assertFalse(self.api.marker.exists())
                self.assertFalse(result_task.done())
                self.screen.send_keys("Enter")
                result = await asyncio.wait_for(result_task, 10)
                self.assertEqual(result.result, "PERMISSION_DONE")
                self.assertFalse(result.is_error)
                self.assertEqual(self.api.marker.read_text(), "approved")
            finally:
                if not result_task.done():
                    result_task.cancel()
                    try:
                        await result_task
                    except asyncio.CancelledError:
                        pass
        self.assertTrue(self.screen.alive)


if __name__ == "__main__":
    unittest.main()
