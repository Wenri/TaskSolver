"""Offline system-message capture, turn boundaries, and client lifecycle checks.

Run: pixi run python test_scripts/test_agy_system_message_api.py
No CLI process, credentials, or model request is used.
"""
from collections import deque
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "antigravity"), str(REPO)]

from pyagy import client
from wirecap.runtime import session as wire_session


def event(text, **fields):
    return dict(kind="system_message", text=text, event_type="agent_message",
                step_address=0x1234, capture_source="system_message", **fields)


def capture_error():
    return dict(kind="system_message_capture_error", capture_source="system_message",
                step_address=0x1234, error="incomplete system-message capture header",
                captured_bytes=16)


def response(objs):
    return client.AgyResponse.from_objs(
        objs, "fallback", exit_status=0, capture_path=None, workspace="/tmp")


class CaptureTests(unittest.TestCase):
    def test_observations_keep_metadata_duplicates_and_empty_text(self):
        note = event("notice", truncated=True, original_text_bytes=100)
        r = response([note, note, event(""), {"kind": "genai_turn", "text": "answer"}])
        self.assertEqual(r.text, "answer")
        self.assertEqual(r.system_messages, [note, note, event("")])
        self.assertTrue(r.system_messages[0]["truncated"])
        self.assertNotIn("conversation_id", r.system_messages[0])

    def test_capture_authority_and_cursor_do_not_replay_earlier_turns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "capture.jsonl"
            first = [event("first"), {"kind": "genai_turn", "text": "recorded"}]
            path.write_text("\n".join(map(json.dumps, first)) + "\n")
            objs, cursor = client._load_capture_objs(path)
            self.assertEqual(objs, first)
            turns, texts, old_cursor = client._load_capture(path)
            self.assertEqual((turns, texts, old_cursor), ([first[1]], [], cursor))
            merged = client._capture_or_stream(
                [event("stream duplicate"), {"kind": "app_response", "text": "app"}], path)
            self.assertEqual(merged, first + [{"kind": "app_response", "text": "app"}])
            with path.open("a") as f:
                f.write("malformed\n" + json.dumps(event("second")) + "\n")
            tail, later = client._load_capture_objs(path, cursor)
            self.assertEqual([o["text"] for o in tail], ["second"])
            self.assertEqual(later, cursor + 2)
            self.assertEqual(client._load_capture_objs(path, later), ([], later))

    def test_capture_disabled_keeps_streamed_system_messages(self):
        objs = [event("streamed"), {"kind": "app_response", "text": "answer"}]
        self.assertEqual(response(client._capture_or_stream(objs, None)).system_messages,
                         [objs[0]])

    def test_capture_errors_keep_stream_and_authoritative_capture(self):
        error = capture_error()
        streamed = [event("notice"), error, {"kind": "app_response", "text": "answer"}]
        self.assertEqual(response(client._capture_or_stream(streamed, None)).system_message_errors,
                         [error])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "capture.jsonl"
            recorded = dict(error, captured_bytes=8)
            path.write_text(json.dumps(recorded) + "\n")
            merged = client._capture_or_stream(streamed, path)
            r = response(merged)
            self.assertEqual(r.system_message_errors, [recorded])
            self.assertEqual(r.system_messages, [event("notice")])
            self.assertEqual(r.text, "answer")
            self.assertEqual(client._load_capture_objs(path, 1), ([], 1))


class Clock:
    now = 0.0

    def time(self):
        return self.now


class ScriptedProcess:
    def __init__(self, clock, queue, script):
        self.clock, self.queue, self.script = clock, queue, deque(script)
        self.last_output = 0.0

    def service_pty(self, wait, readers):
        self.clock.now += 0.1
        while self.script and self.script[0][0] <= self.clock.now:
            _, obj = self.script.popleft()
            self.queue.objects.append(obj)
        return bool(self.queue.objects)

    def submit(self, prompt):
        pass


def scripted_turn(script, **kwargs):
    clock = Clock()
    q = SimpleNamespace(objects=deque())
    q._reader = SimpleNamespace(poll=lambda _: bool(q.objects))
    q.get = q.objects.popleft
    proc = ScriptedProcess(clock, q, script)
    opts = dict(kinds=("answer",), event_kinds=client._EVENT_KINDS, idle=0.25,
                pty_idle=0.8, timeout=1.2, ready=0)
    opts.update(kwargs)
    with mock.patch.object(wire_session, "time", clock):
        objs, reason = wire_session.ask_turn(proc, q, "prompt", **opts)
    return objs, reason, clock.now


class TurnBoundaryTests(unittest.TestCase):
    def test_system_events_do_not_establish_answer_timer(self):
        objs, reason, elapsed = scripted_turn([(0.1, event("early"))])
        self.assertEqual(reason, "idle")
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertEqual([o["text"] for o in objs], ["early"])

    def test_system_events_do_not_extend_answer_timer(self):
        script = [(0.1, {"kind": "answer"}), (0.2, event("a")),
                  (0.3, event("b")), (0.6, event("too late"))]
        objs, reason, elapsed = scripted_turn(script)
        self.assertEqual(reason, "turn")
        self.assertLess(elapsed, 0.6)
        self.assertEqual(len(objs), 3)

    def test_system_event_does_not_clear_pending_tool_result(self):
        script = [(0.1, {"kind": "answer", "pending": True}), (0.2, event("notice"))]
        objs, reason, _ = scripted_turn(
            script, pending=lambda answers: answers[-1].get("pending", False))
        self.assertEqual(reason, "deadline")
        self.assertEqual(len(objs), 2)

    def test_capture_errors_do_not_establish_or_extend_answer_timer(self):
        _, reason, elapsed = scripted_turn([(0.1, capture_error())])
        self.assertEqual(reason, "idle")
        self.assertGreaterEqual(elapsed, 0.8)
        objs, reason, elapsed = scripted_turn([
            (0.1, {"kind": "answer"}), (0.3, capture_error()), (0.6, capture_error())])
        self.assertEqual(reason, "turn")
        self.assertLess(elapsed, 0.6)
        self.assertEqual(len(objs), 2)


class FakeChannel:
    made = []

    def __init__(self, workspace, conversation_id=None):
        self.workspace, self.main = workspace, conversation_id
        self.sent = []
        self.closed = False
        self.started = False
        self.made.append(self)

    def start(self):
        self.started = True
        return {"MESSAGE_CHANNEL": str(id(self))}

    def send(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return str(len(self.sent))

    def status(self, message_id):
        return {"id": message_id, "state": "pending"}

    def bind_main(self, cid):
        self.main = cid

    def close(self):
        self.closed = True


class ClientLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.workspace = self.stack.enter_context(tempfile.TemporaryDirectory())
        FakeChannel.made = []
        self.channel_factory = self.stack.enter_context(
            mock.patch.object(client, "_message_channel", side_effect=FakeChannel))
        self.stack.enter_context(mock.patch.object(client, "ensure_git_workspace",
                                                  side_effect=lambda p: p))
        self.cleanup = mock.Mock()
        self.stack.enter_context(mock.patch.object(client, "_inject_config",
                                                  return_value=self.cleanup))

    def test_session_send_routing_status_and_closed_guard(self):
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

    def test_session_capture_is_per_turn(self):
        s = client.Session(workspace=self.workspace, conversation_id="main",
                           enable_system_messages=False)
        self.addCleanup(s.close)
        s._proc = SimpleNamespace(transcript="", exit_status=None, close=mock.Mock())
        s._turn = mock.Mock(side_effect=[([event("first")], "idle"),
                                        ([event("second")], "idle")])
        self.assertEqual(s.ask("a").system_messages, [event("first")])
        self.assertEqual(s.ask("b").system_messages, [event("second")])
        self.channel_factory.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            s.send_system_message("no")

    def test_session_capture_tail_tracks_system_events_with_same_cursor(self):
        s = client.Session(workspace=self.workspace, enable_system_messages=False)
        self.addCleanup(s.close)
        s.cap_path = str(Path(self.workspace) / "tail.jsonl")
        Path(s.cap_path).write_text(json.dumps(event("first")) + "\n")
        self.assertEqual(s._load_capture_tail(), [event("first")])
        with open(s.cap_path, "a") as f:
            f.write(json.dumps(event("second")) + "\n")
        self.assertEqual(s._load_capture_tail(), [event("second")])

    def test_start_failure_removes_registered_channel(self):
        s = client.Session(workspace=self.workspace)
        q = SimpleNamespace(_writer=SimpleNamespace(close=mock.Mock()))
        proc = SimpleNamespace(_args=(q, client._STREAM_KINDS, None),
                               start=mock.Mock(side_effect=OSError("start failed")),
                               close=mock.Mock())
        with (mock.patch.object(wire_session, "new_channel", return_value=q),
              mock.patch.object(wire_session, "close_channel"),
              mock.patch.object(client, "AgyProcess", return_value=proc)):
            with self.assertRaisesRegex(OSError, "start failed"):
                s.ask("hello")
        self.assertTrue(FakeChannel.made[0].closed)
        self.cleanup.assert_called_once()

    def test_one_shot_captures_final_notifications_and_closes_injection(self):
        q = SimpleNamespace(_writer=SimpleNamespace(close=mock.Mock()))
        proc = SimpleNamespace(transcript="fallback", conversation_id="main",
                               exit_status=None, start=mock.Mock())
        kwargs_seen = {}

        def make_process(**kwargs):
            kwargs_seen.update(kwargs)
            return proc

        def close():
            proc.exit_status = 0
            Path(kwargs_seen["capture"]).write_text(
                json.dumps(event("recorded final")) + "\n")

        proc.close = close
        with (mock.patch.object(client, "_new_channel", return_value=q),
              mock.patch.object(client, "_close_channel"),
              mock.patch.object(client, "AgyProcess", side_effect=make_process),
              mock.patch.object(client, "_collect", return_value=[
                  event("stream copy"), {"kind": "genai_turn", "text": "answer"}])):
            r = client.ask("hello", workspace=self.workspace, system_messages="notice")
        self.assertEqual(r.system_messages, [event("recorded final")])
        self.assertEqual(r.text, "answer")
        self.assertEqual(kwargs_seen["args"][1], client._STREAM_KINDS)
        self.assertEqual(FakeChannel.made[0].sent, [("notice", {})])
        self.assertTrue(FakeChannel.made[0].closed)

    def test_plain_one_shot_has_no_injection_channel(self):
        q = SimpleNamespace(_writer=SimpleNamespace(close=mock.Mock()))
        proc = SimpleNamespace(transcript="", conversation_id="main", exit_status=0,
                               start=mock.Mock(), close=mock.Mock())
        with (mock.patch.object(client, "_new_channel", return_value=q),
              mock.patch.object(client, "_close_channel"),
              mock.patch.object(client, "AgyProcess", return_value=proc),
              mock.patch.object(client, "_collect", return_value=[event("native"), capture_error()])):
            r = client.ask("hello", workspace=self.workspace, capture=False)
        self.assertEqual(r.system_messages, [event("native")])
        self.assertEqual(r.system_message_errors, [capture_error()])
        self.channel_factory.assert_not_called()

    def test_parallel_runs_get_separate_channels_and_preserve_messages(self):
        launches = []

        def make_process(**kwargs):
            launches.append(kwargs)
            return SimpleNamespace(transcript="", conversation_id="main", exit_status=0,
                                   start=mock.Mock(), close=mock.Mock())

        with (mock.patch.object(client, "_new_channel", side_effect=lambda: SimpleNamespace(
                  _writer=SimpleNamespace(close=mock.Mock()))),
              mock.patch.object(client, "_close_channel"),
              mock.patch.object(client, "AgyProcess", side_effect=make_process),
              mock.patch.object(client, "_collect_many", return_value=[
                  [event("first")], [event("second")]])):
            rs = client.ask_many("hello", 2, workspace=self.workspace, capture=False,
                                 system_messages=["seed"])
        self.assertEqual([r.system_messages[0]["text"] for r in rs], ["first", "second"])
        self.assertEqual(len(FakeChannel.made), 2)
        self.assertNotEqual(launches[0]["extra_env"]["MESSAGE_CHANNEL"],
                            launches[1]["extra_env"]["MESSAGE_CHANNEL"])
        self.assertTrue(all(c.closed and c.sent == [("seed", {})] for c in FakeChannel.made))

    def test_parallel_start_failure_closes_all_prepared_channels(self):
        processes = []

        def make_process(**kwargs):
            p = SimpleNamespace(start=mock.Mock(), close=mock.Mock())
            if processes:
                p.start.side_effect = OSError("second start failed")
            processes.append(p)
            return p

        with (mock.patch.object(client, "_new_channel", side_effect=lambda: SimpleNamespace(
                  _writer=SimpleNamespace(close=mock.Mock()))),
              mock.patch.object(client, "_close_channel"),
              mock.patch.object(client, "AgyProcess", side_effect=make_process)):
            with self.assertRaisesRegex(OSError, "second start failed"):
                client.ask_many("hello", 2, workspace=self.workspace, capture=False,
                                system_messages=["seed"])
        self.assertTrue(all(c.closed for c in FakeChannel.made))
        for proc in processes:
            proc.close.assert_called_once()

    def test_parallel_cleanup_error_still_closes_every_channel(self):
        processes, queues, close_attempts = [], [], []
        failure = ValueError("hooks.json malformed")

        def close_channel(channel):
            close_attempts.append(channel)
            channel.closed = True
            # Fail the first close, whichever cleanup order is used.
            if len(close_attempts) == 1:
                raise failure

        def make_queue():
            q = SimpleNamespace(_writer=SimpleNamespace(close=mock.Mock()))
            queues.append(q)
            return q

        def make_process(**kwargs):
            p = SimpleNamespace(transcript="", conversation_id="main", exit_status=0,
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
        self.assertTrue(all(proc.close.called for proc in processes))


class ActualChannelTests(unittest.TestCase):
    def test_session_public_methods_match_actual_channel(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(client, "ensure_git_workspace", side_effect=lambda p: p):
                with client.Session(workspace=d, conversation_id="main") as s:
                    mid = s.send_system_message("harness notice")
                    self.assertEqual(s.system_message_status(mid)["state"], "pending")
                    self.assertEqual(s._messages.main_conversation_id, "main")
                    s._pre_start()
                    config = Path(d) / ".agents" / "hooks.json"
                    self.assertTrue(config.is_file())
            self.assertFalse(config.exists())

    def test_model_metadata_exposes_capture_and_chat_session_disables_injection(self):
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
