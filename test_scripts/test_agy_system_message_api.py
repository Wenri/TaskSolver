"""Offline system-message capture, turn boundaries, and client lifecycle checks.

Run: pixi run python test_scripts/test_agy_system_message_api.py
No CLI process, credentials, or model request is used.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import cast
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "antigravity"), str(REPO)]


from pyagy import client

from wirecap.runtime import session as wire_session
from wirecap.runtime.session import ResultChannel, TurnReason
from wirecap.types import Event


def event(text: str, **fields: object) -> Event:
    return dict(kind="system_message", text=text, event_type="agent_message",
                step_address=0x1234, capture_source="system_message", **fields)


def capture_error() -> Event:
    return dict(kind="system_message_capture_error", capture_source="system_message",
                step_address=0x1234, error="incomplete system-message capture header",
                captured_bytes=16)


def response(objs: list[Event]):
    return client.AgyResponse.from_objs(
        objs, "fallback", exit_status=0, capture_path=None, workspace="/tmp")


class CaptureTests(unittest.TestCase):
    def test_observations_keep_metadata_duplicates_and_empty_text(self) -> None:
        note = event("notice", truncated=True, original_text_bytes=100)
        r = response([note, note, event(""), {"kind": "genai_turn", "text": "answer"}])
        self.assertEqual(r.text, "answer")
        self.assertEqual(r.system_messages, [note, note, event("")])
        self.assertTrue(r.system_messages[0]["truncated"])
        self.assertNotIn("conversation_id", r.system_messages[0])

    def test_capture_authority_and_cursor_do_not_replay_earlier_turns(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "capture.jsonl"
            first: list[Event] = [event("first"), {"kind": "genai_turn", "text": "recorded"}]
            path.write_text("\n".join(map(json.dumps, first)) + "\n")
            objs, cursor = client._load_capture_objs(str(path))
            self.assertEqual(objs, first)
            turns, texts, old_cursor = client._load_capture(str(path))
            self.assertEqual((turns, texts, old_cursor), ([first[1]], [], cursor))
            merged = client._capture_or_stream(
                [event("stream duplicate"), {"kind": "app_response", "text": "app"}], str(path))
            self.assertEqual(merged, first + [{"kind": "app_response", "text": "app"}])
            with path.open("a") as f:
                f.write("malformed\n" + json.dumps(event("second")) + "\n")
            tail, later = client._load_capture_objs(str(path), cursor)
            self.assertEqual([o["text"] for o in tail], ["second"])
            self.assertEqual(later, cursor + 2)
            self.assertEqual(client._load_capture_objs(str(path), later), ([], later))

    def test_capture_skips_nonobject_json_records(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "capture.jsonl"
            path.write_text('["genai_turn"]\n"system_message"\n' + json.dumps(event("valid")) + "\n")
            objs, cursor = client._load_capture_objs(str(path))
            self.assertEqual(objs, [event("valid")])
            self.assertEqual(cursor, 3)

    def test_capture_disabled_keeps_streamed_system_messages(self) -> None:
        objs: list[Event] = [event("streamed"), {"kind": "app_response", "text": "answer"}]
        self.assertEqual(response(client._capture_or_stream(objs, None)).system_messages,
                         [objs[0]])

    def test_capture_errors_keep_stream_and_authoritative_capture(self) -> None:
        error = capture_error()
        streamed: list[Event] = [event("notice"), error, {"kind": "app_response", "text": "answer"}]
        self.assertEqual(response(client._capture_or_stream(streamed, None)).system_message_errors,
                         [error])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "capture.jsonl"
            recorded = dict(error, captured_bytes=8)
            path.write_text(json.dumps(recorded) + "\n")
            merged = client._capture_or_stream(streamed, str(path))
            r = response(merged)
            self.assertEqual(r.system_message_errors, [recorded])
            self.assertEqual(r.system_messages, [event("notice")])
            self.assertEqual(r.text, "answer")
            self.assertEqual(client._load_capture_objs(str(path), 1), ([], 1))


class Clock:
    now = 0.0

    def time(self):
        return self.now


class ScriptedProcess:
    def __init__(self, clock: Clock, queue: ResultChannel,
                 script: Sequence[tuple[float, Event]]) -> None:
        self.clock, self.queue, self.script = clock, queue, deque(script)
        self.last_output = 0.0

    def service_pty(self, timeout: float, readers: Sequence[Connection | int]) -> bool:
        self.clock.now += 0.1
        while self.script and self.script[0][0] <= self.clock.now:
            _, obj = self.script.popleft()
            self.queue.put(obj)
        return self.queue._reader.poll(0)

    def submit(self, prompt: str) -> None:
        pass

    def write(self, data: bytes) -> None:
        raise AssertionError("scripted turns always submit a prompt")

    def reap(self) -> None:
        raise AssertionError("scripted processes remain alive")


def scripted_turn(script: Sequence[tuple[float, Event]], *,
                  pending: Callable[[list[Event]], bool] | None = None
                  ) -> tuple[list[Event], TurnReason, float]:
    clock = Clock()
    q = wire_session.new_channel()
    proc = ScriptedProcess(clock, q, script)
    try:
        with mock.patch.object(wire_session, "time", clock):
            objs, reason = wire_session.ask_turn(
                proc, q, "prompt", kinds=("answer",), event_kinds=client._EVENT_KINDS,
                idle=0.25, pty_idle=0.8, timeout=1.2, ready=0, pending=pending)
        return objs, reason, clock.now
    finally:
        wire_session.close_channel(q)


class TurnBoundaryTests(unittest.TestCase):
    def test_system_events_do_not_establish_answer_timer(self) -> None:
        objs, reason, elapsed = scripted_turn([(0.1, event("early"))])
        self.assertEqual(reason, "idle")
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertEqual([o["text"] for o in objs], ["early"])

    def test_system_events_do_not_extend_answer_timer(self) -> None:
        script: list[tuple[float, Event]] = [(0.1, {"kind": "answer"}), (0.2, event("a")),
                  (0.3, event("b")), (0.6, event("too late"))]
        objs, reason, elapsed = scripted_turn(script)
        self.assertEqual(reason, "turn")
        self.assertLess(elapsed, 0.6)
        self.assertEqual(len(objs), 3)

    def test_system_event_does_not_clear_pending_tool_result(self) -> None:
        script: list[tuple[float, Event]] = [(0.1, {"kind": "answer", "pending": True}), (0.2, event("notice"))]
        objs, reason, _ = scripted_turn(
            script, pending=lambda answers: bool(answers[-1].get("pending", False)))
        self.assertEqual(reason, "deadline")
        self.assertEqual(len(objs), 2)

    def test_capture_errors_do_not_establish_or_extend_answer_timer(self) -> None:
        _, reason, elapsed = scripted_turn([(0.1, capture_error())])
        self.assertEqual(reason, "idle")
        self.assertGreaterEqual(elapsed, 0.8)
        objs, reason, elapsed = scripted_turn([
            (0.1, {"kind": "answer"}), (0.3, capture_error()), (0.6, capture_error())])
        self.assertEqual(reason, "turn")
        self.assertLess(elapsed, 0.6)
        self.assertEqual(len(objs), 2)


class FakeChannel:
    made: list[FakeChannel] = []

    def __init__(self, workspace: str, conversation_id: str | None = None) -> None:
        self.workspace, self.main = workspace, conversation_id
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        self.started = False
        self.made.append(self)

    def start(self):
        self.started = True
        return {"MESSAGE_CHANNEL": str(id(self))}

    def send(self, text: str, **kwargs: object) -> str:
        self.sent.append((text, kwargs))
        return str(len(self.sent))

    def status(self, message_id: str):
        return {"id": message_id, "state": "pending"}

    def bind_main(self, cid: str) -> None:
        self.main = cid

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeWriter:
    close: mock.Mock = field(default_factory=mock.Mock)


@dataclass
class FakeQueue:
    _writer: FakeWriter = field(default_factory=FakeWriter)


@dataclass
class FakeProcess:
    transcript: str = ""
    conversation_id: str = "main"
    exit_status: int | None = 0
    _args: tuple[object, ...] = ()
    start: mock.Mock = field(default_factory=mock.Mock)
    reap: mock.Mock = field(default_factory=mock.Mock)
    close: mock.Mock = field(default_factory=mock.Mock)


class ClientLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.workspace = self.stack.enter_context(tempfile.TemporaryDirectory())
        FakeChannel.made = []
        self.channel_factory = self.stack.enter_context(
            mock.patch.object(client, "_message_channel", side_effect=FakeChannel))
        self.stack.enter_context(mock.patch.object(client, "ensure_git_workspace",
                                                  side_effect=str))
        self.cleanup = mock.Mock()
        self.stack.enter_context(mock.patch.object(client, "_inject_config",
                                                  return_value=self.cleanup))

    def test_session_send_routing_status_and_closed_guard(self) -> None:
        s = client.Session(workspace=self.workspace, conversation_id="main",
                           system_messages=["initial"])
        self.addCleanup(s.close)
        channel = FakeChannel.made[0]
        self.assertEqual(s.system_message_ids, ["1"])
        self.assertEqual(s.send_system_message("next", conversation_id="child"), "2")
        self.assertEqual(channel.sent[-1], ("next", {
            "conversation_id": "child"}))
        self.assertEqual(s.system_message_status("2")["state"], "pending")
        s._pre_start()
        self.assertTrue(channel.started)
        self.assertIn("MESSAGE_CHANNEL", s._overlays)
        s.close()
        self.assertTrue(channel.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            s.send_system_message("late")

    def test_session_capture_path_and_disabled_native_destination(self) -> None:
        custom = str(Path(self.workspace) / "captures" / "session.jsonl")
        for capture in (custom, False):
            with self.subTest(capture=capture):
                session = client.Session(workspace=self.workspace, capture=capture,
                                         enable_system_messages=False)
                self.addCleanup(session.close)
                session._pre_start()
                expected = custom if isinstance(capture, str) else None
                self.assertEqual(session.cap_path, expected)
                process = session._make_process("hello")
                self.assertEqual(process._capture, expected or os.devnull)
        self.assertTrue(Path(custom).is_file())
        self.assertFalse((Path(self.workspace) / "pyagy-session.jsonl").exists())

    def test_parallel_custom_capture_paths_are_distinct(self) -> None:
        custom = str(Path(self.workspace) / "captures" / "sample.jsonl")
        launches: list[dict[str, object]] = []

        def make_process(**kwargs: object) -> FakeProcess:
            launches.append(kwargs)
            return FakeProcess()

        with (mock.patch.object(client, "_new_channel", side_effect=FakeQueue),
              mock.patch.object(client, "_close_channel"),
              mock.patch.object(client, "AgyProcess", side_effect=make_process),
              mock.patch.object(client, "_collect_many", return_value=[[], []])):
            responses = client.ask_many("hello", 2, workspace=self.workspace, capture=custom)
        expected = [str(Path(self.workspace) / "captures" / f"sample-{i}.jsonl") for i in range(2)]
        self.assertEqual([r.capture_path for r in responses], expected)
        self.assertEqual([options["capture"] for options in launches], expected)
        self.assertTrue(all(Path(path).is_file() for path in expected))

    def test_session_capture_is_per_turn(self) -> None:
        s = client.Session(workspace=self.workspace, conversation_id="main",
                           enable_system_messages=False)
        self.addCleanup(s.close)
        self.stack.enter_context(mock.patch.object(s, "_proc",
                                                  FakeProcess(transcript="", exit_status=None)))
        s._turn = mock.Mock(side_effect=[([event("first")], "idle"),
                                        ([event("second")], "idle")])
        self.assertEqual(s.ask("a").system_messages, [event("first")])
        self.assertEqual(s.ask("b").system_messages, [event("second")])
        self.channel_factory.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            s.send_system_message("no")

    def test_session_capture_tail_tracks_system_events_with_same_cursor(self) -> None:
        s = client.Session(workspace=self.workspace, enable_system_messages=False)
        self.addCleanup(s.close)
        s.cap_path = str(Path(self.workspace) / "tail.jsonl")
        Path(s.cap_path).write_text(json.dumps(event("first")) + "\n")
        self.assertEqual(s._load_capture_tail(), [event("first")])
        with open(s.cap_path, "a") as f:
            f.write(json.dumps(event("second")) + "\n")
        self.assertEqual(s._load_capture_tail(), [event("second")])

    def test_start_failure_removes_registered_channel(self) -> None:
        s = client.Session(workspace=self.workspace)
        q = FakeQueue()
        proc = FakeProcess(_args=(q, client._STREAM_KINDS, None),
                               start=mock.Mock(side_effect=OSError("start failed")),
                               close=mock.Mock())
        with (mock.patch.object(wire_session, "new_channel", return_value=q),
              mock.patch.object(wire_session, "close_channel"),
              mock.patch.object(client, "AgyProcess", return_value=proc)):
            with self.assertRaisesRegex(OSError, "start failed"):
                s.ask("hello")
        self.assertTrue(FakeChannel.made[0].closed)
        self.cleanup.assert_called_once()

    def test_one_shot_captures_final_notifications_and_closes_injection(self) -> None:
        q = FakeQueue()
        proc = FakeProcess(transcript="fallback", conversation_id="main",
                               exit_status=None, start=mock.Mock())
        kwargs_seen: dict[str, object] = {}

        def make_process(**kwargs: object):
            kwargs_seen.update(kwargs)
            return proc

        def close() -> None:
            proc.exit_status = 0
            capture = kwargs_seen["capture"]
            assert isinstance(capture, str)
            Path(capture).write_text(
                json.dumps(event("recorded final")) + "\n")

        cast(mock.Mock, proc.close).side_effect = close
        with (mock.patch.object(client, "_new_channel", return_value=q),
              mock.patch.object(client, "_close_channel"),
              mock.patch.object(client, "AgyProcess", side_effect=make_process),
              mock.patch.object(client, "_collect", return_value=[
                  event("stream copy"), {"kind": "genai_turn", "text": "answer"}])):
            r = client.ask("hello", workspace=self.workspace, system_messages="notice")
        self.assertEqual(r.system_messages, [event("recorded final")])
        self.assertEqual(r.text, "answer")
        self.assertEqual(cast(tuple[object, ...], kwargs_seen["args"])[1], client._STREAM_KINDS)
        self.assertEqual(FakeChannel.made[0].sent, [("notice", {})])
        self.assertTrue(FakeChannel.made[0].closed)

    def test_plain_one_shot_has_no_injection_channel(self) -> None:
        q = FakeQueue()
        proc = FakeProcess(transcript="", conversation_id="main", exit_status=0,
                               start=mock.Mock(), close=mock.Mock())
        with (mock.patch.object(client, "_new_channel", return_value=q),
              mock.patch.object(client, "_close_channel"),
              mock.patch.object(client, "AgyProcess", return_value=proc),
              mock.patch.object(client, "_collect", return_value=[event("native"), capture_error()])):
            r = client.ask("hello", workspace=self.workspace, capture=False)
        self.assertEqual(r.system_messages, [event("native")])
        self.assertEqual(r.system_message_errors, [capture_error()])
        self.channel_factory.assert_not_called()

    def test_parallel_runs_get_separate_channels_and_preserve_messages(self) -> None:
        launches: list[dict[str, object]] = []

        def make_process(**kwargs: object):
            launches.append(kwargs)
            return FakeProcess(transcript="", conversation_id="main", exit_status=0,
                                   start=mock.Mock(), close=mock.Mock())

        with (mock.patch.object(client, "_new_channel", side_effect=FakeQueue),
              mock.patch.object(client, "_close_channel"),
              mock.patch.object(client, "AgyProcess", side_effect=make_process),
              mock.patch.object(client, "_collect_many", return_value=[
                  [event("first")], [event("second")]])):
            rs = client.ask_many("hello", 2, workspace=self.workspace, capture=False,
                                 system_messages=["seed"])
        self.assertEqual([r.system_messages[0]["text"] for r in rs], ["first", "second"])
        self.assertEqual([options["capture"] for options in launches], [os.devnull, os.devnull])
        self.assertEqual(len(FakeChannel.made), 2)
        self.assertNotEqual(cast(dict[str, str], launches[0]["extra_env"])["MESSAGE_CHANNEL"],
                            cast(dict[str, str], launches[1]["extra_env"])["MESSAGE_CHANNEL"])
        self.assertTrue(all(c.closed and c.sent == [("seed", {})] for c in FakeChannel.made))

    def test_parallel_start_failure_closes_all_prepared_channels(self) -> None:
        processes: list[FakeProcess] = []

        def make_process(**kwargs: object):
            p = FakeProcess(start=mock.Mock(), close=mock.Mock())
            if processes:
                cast(mock.Mock, p.start).side_effect = OSError("second start failed")
            processes.append(p)
            return p

        with (mock.patch.object(client, "_new_channel", side_effect=FakeQueue),
              mock.patch.object(client, "_close_channel"),
              mock.patch.object(client, "AgyProcess", side_effect=make_process)):
            with self.assertRaisesRegex(OSError, "second start failed"):
                client.ask_many("hello", 2, workspace=self.workspace, capture=False,
                                system_messages=["seed"])
        self.assertTrue(all(c.closed for c in FakeChannel.made))
        for proc in processes:
            cast(mock.Mock, proc.close).assert_called_once()

    def test_parallel_cleanup_error_still_closes_every_channel(self) -> None:
        processes: list[FakeProcess] = []
        queues: list[FakeQueue] = []
        close_attempts: list[FakeChannel] = []
        failure = ValueError("hooks.json malformed")

        def close_channel(channel: FakeChannel) -> None:
            close_attempts.append(channel)
            channel.closed = True
            # Fail the first close, whichever cleanup order is used.
            if len(close_attempts) == 1:
                raise failure

        def make_queue():
            q = FakeQueue()
            queues.append(q)
            return q

        def make_process(**kwargs: object):
            p = FakeProcess(transcript="", conversation_id="main", exit_status=0,
                                start=mock.Mock(), close=mock.Mock())
            processes.append(p)
            return p

        with (mock.patch.object(client, "_new_channel", side_effect=make_queue),
              mock.patch.object(client, "_close_channel") as close_queue,
              mock.patch.object(client, "AgyProcess", side_effect=make_process),
              mock.patch.object(client, "_collect_many", return_value=[[], [], []]),
              mock.patch.object(FakeChannel, "close", close_channel)):
            with self.assertRaises(ValueError) as raised:
                client.ask_many("hello", 3, workspace=self.workspace, capture=False,
                                system_messages=["seed"])
        self.assertIs(raised.exception, failure)
        self.assertCountEqual(close_attempts, FakeChannel.made)
        self.assertTrue(all(channel.closed for channel in FakeChannel.made))
        self.assertCountEqual(close_queue.call_args_list, [mock.call(q) for q in queues])
        self.cleanup.assert_called_once()
        self.assertTrue(all(cast(mock.Mock, proc.close).called for proc in processes))


class ConfigCompositionTests(unittest.TestCase):
    def test_scoped_tools_and_external_servers_share_one_private_config(self) -> None:
        from wirecap.types import json_loads_object, json_object
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            global_config = root / "global.json"
            original = '{"mcpServers": {"existing": {"command": "keep"}}}'
            global_config.write_text(original)
            data_dir = root / "scoped"
            with (mock.patch.dict(os.environ, {"AGY_MCP_CONFIG_PATH": str(global_config)}),
                  mock.patch.object(client._conv, "prepare_scoped_home")):
                cleanup = client._inject_config(
                    [client.ToolSpec("weather", response="dry")], None,
                    {"external": {"command": "server"}}, str(data_dir))
                path = data_dir / ".gemini/config/mcp_config.json"
                servers = json_object(json_loads_object(path.read_text())["mcpServers"])
                self.assertEqual(len(servers), 2)
                self.assertIn("external", servers)
                self.assertEqual(global_config.read_text(), original)
                cleanup()
                remaining = json_object(json_loads_object(path.read_text())["mcpServers"])
                self.assertEqual(list(remaining), ["external"])

    def test_global_servers_restore_previous_values_and_keep_concurrent_edits(self) -> None:
        from wirecap.types import json_loads_object, json_object
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            original = {"command": "original", "custom": {"preserve": True}}
            path.write_text(json.dumps({"setting": "before", "mcpServers": {"shared": original}}))
            with mock.patch.dict(os.environ, {"AGY_MCP_CONFIG_PATH": str(path)}):
                cleanup = client._inject_config(None, None, {
                    "shared": {"command": "temporary"}, "new": {"command": "temporary-new"}})
                current = json_loads_object(path.read_text())
                current["setting"] = "edited during run"
                json_object(current["mcpServers"])["external"] = {"command": "added-during-run"}
                path.write_text(json.dumps(current))
                cleanup()
                restored = json_loads_object(path.read_text())
                self.assertEqual(restored["setting"], "edited during run")
                self.assertEqual(json_object(restored["mcpServers"]), {
                    "shared": original, "external": {"command": "added-during-run"}})
                cleanup()  # Repeated cleanup cannot remove the restored original.
                self.assertEqual(json_loads_object(path.read_text()), restored)

                cleanup = client._inject_config(None, None, {"shared": {"command": "temporary"}})
                current = json_loads_object(path.read_text())
                entries = json_object(current["mcpServers"])
                entries["shared"] = {"command": "user-edited"}
                path.write_text(json.dumps(current))
                cleanup()
                self.assertEqual(json_object(json_loads_object(path.read_text())["mcpServers"])["shared"],
                                 {"command": "user-edited"})

                cleanup = client._inject_config(None, None, {"shared": {"command": "temporary"}})
                path.unlink()
                cleanup()
                self.assertFalse(path.exists())

    def test_overlapping_global_server_overlays_close_in_either_order(self) -> None:
        from wirecap.types import json_loads_object, json_object
        for reverse in (False, True):
            for same_spec in (False, True):
                with self.subTest(reverse=reverse, same_spec=same_spec), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "mcp.json"
                    original = {"command": "original"}
                    path.write_text(json.dumps({"mcpServers": {"shared": original}}))
                    with mock.patch.dict(os.environ, {"AGY_MCP_CONFIG_PATH": str(path)}):
                        first = client._inject_config(None, None, {"shared": {"command": "first"}})
                        first_value = json_object(json_loads_object(path.read_text())["mcpServers"])["shared"]
                        second = client._inject_config(None, None, {
                            "shared": {"command": "first" if same_spec else "second"}})
                        second_value = json_object(json_loads_object(path.read_text())["mcpServers"])["shared"]
                        close_one, close_two = (second, first) if reverse else (first, second)
                        close_one()
                        self.assertEqual(json_object(json_loads_object(path.read_text())["mcpServers"])["shared"],
                                         first_value if reverse else second_value)
                        close_two()
                        self.assertEqual(json_object(json_loads_object(path.read_text())["mcpServers"])["shared"],
                                         original)
                        self.assertNotIn(str(path), client._MCP_OVERLAYS)

    def test_config_failure_removes_already_registered_tools(self) -> None:
        from wirecap.types import json_loads_object, json_object
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mcp.json"
            with (mock.patch.dict(os.environ, {"AGY_MCP_CONFIG_PATH": str(path)}),
                  mock.patch.object(client._config, "write_mcp_servers", side_effect=ValueError("bad server"))):
                with self.assertRaisesRegex(ValueError, "bad server"):
                    client._inject_config([client.ToolSpec("weather", response="dry")], None,
                                          {"external": {"command": "server"}})
            self.assertEqual(json_object(json_loads_object(path.read_text())["mcpServers"]), {})


class ActualChannelTests(unittest.TestCase):
    def test_session_public_methods_match_actual_channel(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(client, "ensure_git_workspace", side_effect=str):
                with client.Session(workspace=d, conversation_id="main") as s:
                    mid = s.send_system_message("harness notice")
                    self.assertEqual(s.system_message_status(mid)["state"], "pending")
                    assert s._messages is not None
                    self.assertEqual(s._messages.main_conversation_id, "main")
                    s._pre_start()
                    config = Path(d) / ".agents" / "hooks.json"
                    self.assertTrue(config.is_file())
            self.assertFalse(config.exists())

    def test_model_metadata_exposes_capture_and_chat_session_disables_injection(self) -> None:
        from pyagy.model import AgyModel
        with tempfile.TemporaryDirectory() as d:
            model = AgyModel(chat=True, workspace=d)
            r = response([event("notice"), capture_error()])
            self.assertEqual(model._finish(r)["system_messages"], [event("notice")])
            self.assertEqual(model._finish(r)["system_message_errors"], [capture_error()])
            with model.session() as s:
                self.assertIsNone(s._messages)
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    s.send_system_message("no injection in chat mode")


if __name__ == "__main__":
    unittest.main()
