"""Offline agy system-message queue/routing tests; no model calls or credentials."""
import concurrent.futures
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "antigravity"))

from pyagy.system_messages import (CHANNEL_ENV, MAX_BATCH_BYTES, MAX_MESSAGE_BYTES,  # noqa: E402
                                  SystemMessageChannel, _deliver)
from pyagy import system_messages  # noqa: E402


class SystemMessageQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.channels = []

    def tearDown(self):
        for channel in self.channels:
            channel.close()
        self.directory.cleanup()

    def channel(self, **kwargs):
        channel = SystemMessageChannel(self.workspace, **kwargs)
        self.channels.append(channel)
        return channel

    def invoke(self, channel, payload, *, env=None):
        overlay = channel.start()
        config = json.loads((self.workspace / ".agents" / "hooks.json").read_text())
        command = config[channel._key]["PreInvocation"][0]["command"]
        proc = subprocess.run(shlex.split(command), input=json.dumps(payload), text=True,
                              capture_output=True, env={**os.environ, **(overlay if env is None else env)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_native_json_contract_and_main_child_routing(self):
        channel = self.channel()
        main = channel.send("Harness: keep the answer short.")
        child = channel.send("Check the parser.", conversation_id="child")
        # Explicit native parent/root metadata identifies this as a child even
        # when its callback arrives before the main conversation's callback.
        result = self.invoke(channel, {"conversationId": "child", "parentConversationId": "main",
                                       "rootConversationId": "main"})
        self.assertEqual(result["injectSteps"][0]["systemMessage"]["systemMessage"], "Check the parser.")
        self.assertEqual(channel.status(child)["state"], "emitted")
        self.assertEqual(channel.status(main)["state"], "pending")
        result = self.invoke(channel, {"conversationId": "main"})
        self.assertEqual(result, {"injectSteps": [{"systemMessage": {
            "systemMessage": "Harness: keep the answer short.",
        }}]})
        self.assertEqual(self.invoke(channel, {"conversationId": "main"}), {})
        self.assertEqual(channel.main_conversation_id, "main")

    def test_first_invocation_latch_and_resume_binding(self):
        channel = self.channel()
        channel.send("main only")
        self.invoke(channel, {"conversationId": "main"})
        child_message = channel.send("later main only")
        self.assertEqual(self.invoke(channel, {"conversationId": "child"}), {})
        self.assertEqual(channel.status(child_message)["state"], "pending")
        with self.assertRaisesRegex(ValueError, "another conversation"):
            channel.bind_main("other")
        resumed = self.channel(conversation_id="resumed")
        resumed.send("resume only")
        self.assertEqual(self.invoke(resumed, {"conversationId": "child"}), {})
        self.assertIn("injectSteps", self.invoke(resumed, {"conversationId": "resumed"}))

    def test_process_scope_prevents_other_sessions_consuming_queue(self):
        first, second = self.channel(), self.channel()
        message = first.send("belongs to first")
        first.start()
        second_env = second.start()
        self.assertEqual(self.invoke(first, {"conversationId": "other"}, env=second_env), {})
        self.assertEqual(first.status(message)["state"], "pending")
        self.assertIsNone(first.main_conversation_id)
        self.assertIn("injectSteps", self.invoke(first, {"conversationId": "first"}))

    def test_concurrent_callbacks_emit_each_message_at_most_once(self):
        channel = self.channel(conversation_id="main")
        texts = {f"notification {i}" for i in range(20)}
        ids = {channel.send(text) for text in texts}
        channel.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.invoke(channel, {"conversationId": "main"}), range(4)))
        emitted = [step["systemMessage"]["systemMessage"]
                   for result in results for step in result.get("injectSteps", [])]
        self.assertEqual(set(emitted), texts)
        self.assertEqual(len(emitted), len(ids))

    def test_registration_cleanup_preserves_user_and_concurrent_edits(self):
        path = self.workspace / ".agents" / "hooks.json"
        path.parent.mkdir()
        original = {"user-hook": {"PreInvocation": [{"command": "true"}]}}
        path.write_text(json.dumps(original))
        first, second = self.channel(), self.channel()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda channel: channel.start(), (first, second)))
        config = json.loads(path.read_text())
        self.assertEqual(set(config), {"user-hook", first._key, second._key})
        config["new-user-hook"] = {"enabled": False}
        path.write_text(json.dumps(config))
        first.close()
        config = json.loads(path.read_text())
        self.assertIn(second._key, config)
        self.assertEqual(config["new-user-hook"], {"enabled": False})
        second.close()
        self.assertEqual(json.loads(path.read_text()), {**original, "new-user-hook": {"enabled": False}})

    def test_cleanup_restores_absence_and_preserves_modified_own_entry(self):
        channel = self.channel()
        channel.start()
        channel.close()
        self.assertFalse((self.workspace / ".agents").exists())
        modified = self.channel()
        modified.start()
        path = self.workspace / ".agents" / "hooks.json"
        config = json.loads(path.read_text())
        config[modified._key]["enabled"] = False
        path.write_text(json.dumps(config))
        modified.close()
        self.assertEqual(json.loads(path.read_text()), config)

    def test_concurrent_channels_restore_absence_in_either_close_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                channels = [self.channel(), self.channel()]
                for channel in channels:
                    channel.start()
                for channel in reversed(channels) if reverse else channels:
                    channel.close()
                self.assertFalse((self.workspace / ".agents").exists())

    def test_cleanup_removes_private_queue_after_user_corrupts_config(self):
        channel = self.channel()
        channel.start()
        path = self.workspace / ".agents" / "hooks.json"
        path.write_text("user is editing this")
        with self.assertRaises(ValueError):
            channel.close()
        self.assertEqual(path.read_text(), "user is editing this")
        self.assertFalse(channel._queue.exists())
        channel.close()

    def test_invalid_config_is_not_overwritten(self):
        path = self.workspace / ".agents" / "hooks.json"
        path.parent.mkdir()
        for content in ("[]", "not valid json"):
            path.write_text(content)
            with self.assertRaises(ValueError):
                self.channel().start()
            self.assertEqual(path.read_text(), content)

    def test_failed_registration_rolls_back_shared_provenance_and_directory(self):
        channel = self.channel()
        write = system_messages._write

        def fail_config(path, value, **kwargs):
            if Path(path) == channel._config:
                raise OSError("simulated config replacement failure")
            return write(path, value, **kwargs)

        with mock.patch.object(system_messages, "_write", side_effect=fail_config):
            with self.assertRaisesRegex(OSError, "replacement failure"):
                channel.start()
        self.assertFalse(channel._registration.exists())
        self.assertFalse(channel._config.parent.exists())
        channel.close()
        self.assertFalse(channel._queue.exists())

    def test_config_retry_merges_edits_during_start_and_close(self):
        path = self.workspace / ".agents" / "hooks.json"
        path.parent.mkdir()
        path.write_text('{"user-hook": {}}')
        for operation in ("start", "close"):
            with self.subTest(operation=operation):
                channel = self.channel()
                if operation == "close":
                    channel.start()
                check = system_messages._check_config
                calls = []

                def edit_before_check(target, expected):
                    calls.append(target)
                    if len(calls) == 1:
                        config = json.loads(path.read_text())
                        config["edited-during-" + operation] = {"command": "true"}
                        path.write_text(json.dumps(config))
                    return check(target, expected)

                with mock.patch.object(system_messages, "_check_config", edit_before_check):
                    getattr(channel, operation)()
                config = json.loads(path.read_text())
                self.assertIn("user-hook", config)
                self.assertIn("edited-during-" + operation, config)
                self.assertEqual(channel._key in config, operation == "start")
                self.assertEqual(len(calls), 2)
                self.assertEqual(list(path.parent.glob(".write-*")), [])
                channel.close()

    def test_cleanup_retries_unlink_after_concurrent_config_creation(self):
        channel = self.channel()
        channel.start()
        check = system_messages._check_config
        calls = []

        def edit_before_unlink(path, expected):
            calls.append(path)
            if len(calls) == 1:
                config = json.loads(path.read_text())
                config["user-hook"] = {}
                path.write_text(json.dumps(config))
            return check(path, expected)

        with mock.patch.object(system_messages, "_check_config", edit_before_unlink):
            channel.close()
        self.assertEqual(json.loads(channel._config.read_text()), {"user-hook": {}})
        self.assertEqual(len(calls), 2)

    def test_repeated_config_conflicts_stop_without_overwriting_user_edits(self):
        for operation in ("start", "close"):
            with self.subTest(operation=operation):
                channel = self.channel()
                path = channel._config
                path.parent.mkdir(exist_ok=True)
                path.write_text('{"user-hook": {}}')
                if operation == "close":
                    channel.start()
                registry = channel._registration.read_bytes() if channel._registration.exists() else None
                check = system_messages._check_config
                calls = []

                def keep_editing(target, expected):
                    calls.append(target)
                    config = json.loads(path.read_text())
                    config["edit-number"] = len(calls)
                    path.write_text(json.dumps(config))
                    return check(target, expected)

                with mock.patch.object(system_messages, "_check_config", keep_editing):
                    with self.assertRaisesRegex(RuntimeError, "hooks.json changed"):
                        getattr(channel, operation)()
                config = json.loads(path.read_text())
                self.assertEqual(config["edit-number"], system_messages.CONFIG_RETRIES)
                self.assertEqual(channel._key in config, operation == "close")
                self.assertEqual(channel._registration.read_bytes() if channel._registration.exists()
                                 else None, registry)
                self.assertEqual(list(path.parent.glob(".write-*")), [])
                channel.close()
                self.assertFalse(channel._queue.exists())
                # An exhausted close deliberately leaves the shared registration
                # unchanged. Remove this test's orphan outside the mocked race.
                channel._registration.unlink(missing_ok=True)

    def test_escaped_message_limit_is_checked_before_enqueue(self):
        channel = self.channel(conversation_id="main")
        with self.assertRaisesRegex(ValueError, "encoded hook-output bytes"):
            channel.send("\0" * MAX_MESSAGE_BYTES)
        self.assertEqual(list((channel._queue / "messages").glob("*.json")), [])
        overhead = len((json.dumps({"injectSteps": [{"systemMessage": {
            "systemMessage": ""}}]}, ensure_ascii=False) + "\n").encode("utf-8"))
        zeros, remainder = divmod(MAX_BATCH_BYTES - overhead, 6)
        text = "\0" * zeros + "x" * remainder
        message = channel.send(text)
        with self.assertRaisesRegex(ValueError, "encoded hook-output bytes"):
            channel.send(text + "x")
        output = io.StringIO()
        _deliver(channel._queue, {"conversationId": "main"}, output)
        self.assertEqual(len(output.getvalue().encode("utf-8")), MAX_BATCH_BYTES)
        self.assertEqual(json.loads(output.getvalue())["injectSteps"][0]["systemMessage"]
                         ["systemMessage"], text)
        self.assertEqual(channel.status(message)["state"], "emitted")

    def test_batch_limit_counts_utf8_separators_wrapper_and_newline(self):
        channel = self.channel(conversation_id="main")
        texts = ["first\0", "second é", "third"]
        steps = [{"systemMessage": {"systemMessage": text}} for text in texts[:2]]
        limit = len((json.dumps({"injectSteps": steps}, ensure_ascii=False) + "\n").encode("utf-8"))
        with mock.patch.object(system_messages, "MAX_BATCH_BYTES", limit):
            ids = [channel.send(text) for text in texts]
            output = io.StringIO()
            _deliver(channel._queue, {"conversationId": "main"}, output)
            self.assertEqual(len(output.getvalue().encode("utf-8")), limit)
            self.assertEqual(json.loads(output.getvalue())["injectSteps"], steps)
            self.assertEqual([channel.status(mid)["state"] for mid in ids],
                             ["emitted", "emitted", "pending"])
            output = io.StringIO()
            _deliver(channel._queue, {"conversationId": "main"}, output)
            self.assertLessEqual(len(output.getvalue().encode("utf-8")), limit)
            self.assertEqual(channel.status(ids[2])["state"], "emitted")

    def test_failed_stdout_stays_claimed_without_automatic_duplicate(self):
        class BrokenOutput:
            def write(self, value):
                raise BrokenPipeError("gone")

        channel = self.channel(conversation_id="main")
        message = channel.send("do not duplicate")
        with self.assertRaises(BrokenPipeError):
            _deliver(channel._queue, {"conversationId": "main"}, BrokenOutput())
        self.assertEqual(channel.status(message)["state"], "claimed")
        output = io.StringIO()
        _deliver(channel._queue, {"conversationId": "main"}, output)
        self.assertEqual(json.loads(output.getvalue()), {})

    def test_validation_and_closed_channel(self):
        channel = self.channel()
        for text in ("", "  ", None, "x" * (MAX_MESSAGE_BYTES + 1)):
            with self.assertRaises(ValueError):
                channel.send(text)
        with self.assertRaises(ValueError):
            channel.send("hello", conversation_id="")
        with self.assertRaises(ValueError):
            channel.status("../state")
        channel.close()
        with self.assertRaises(RuntimeError):
            channel.send("hello")


if __name__ == "__main__":
    unittest.main()
