#!/usr/bin/env python3
"""Kimi Code's capture hooks end to end, against a loopback model.

The built bundle and its wirecap addon run one print-mode turn through ``pykimi.ask``. The model
is an OpenAI-compatible chat-completions fixture on 127.0.0.1, defined by the ``KIMI_MODEL_*``
variables, with the store scoped by ``kimi_home``. The capture must hold what TaskSolver's wiretap
patch emits: the request with the prompt, wire-journal records, and the turn pykimi decodes from
the streamed events. A hook that an adoption dropped or misplaced fails here; ``--version`` checks
never send a request. No credentials and no network beyond loopback.

    pixi run python test_scripts/test_kimi_capture.py
"""

from __future__ import annotations

import collections
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

import pykimi
from pykimi._env import KIMI_MAIN, WIRE_NODE_ADDON
from wirecap.types import JSONObject, json_array, json_loads_object, json_object

PROMPT: Final = "Reply with the loopback answer."
ANSWER: Final = "Loopback answer from the fixture."
MODEL: Final = "loopback-model"
USAGE: Final = {"prompt_tokens": 21, "completion_tokens": 6, "total_tokens": 27}


class LoopbackModel:
    """OpenAI chat completions on 127.0.0.1: every request gets ANSWER, then USAGE."""

    def __init__(self) -> None:
        self.requests: list[JSONObject] = []
        self.paths: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                owner.respond(self)

            def do_GET(self) -> None:
                owner.paths.append(f"GET {self.path}")
                owner.send(self, 404, b"{}", "application/json")

        self.server: Final = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread: Final = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        self.paths.append(f"POST {handler.path}")
        length = int(handler.headers.get("Content-Length") or 0)
        body = json_loads_object(handler.rfile.read(length))
        if not handler.path.endswith("/chat/completions"):
            self.send(handler, 404, b"{}", "application/json")
            return
        self.requests.append(body)
        if body.get("stream") is not True:
            message: dict[str, object] = {
                "id": "chatcmpl-loopback", "object": "chat.completion", "created": 0, "model": MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                             "finish_reason": "stop"}],
                "usage": USAGE}
            self.send(handler, 200, json.dumps(message).encode(), "application/json")
            return
        chunk: dict[str, object] = {"id": "chatcmpl-loopback", "object": "chat.completion.chunk",
                                    "created": 0, "model": MODEL}
        deltas: tuple[dict[str, str], ...] = (
            {"role": "assistant", "content": ""}, {"content": ANSWER[:9]}, {"content": ANSWER[9:]}, {})
        events = [chunk | {"choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if not delta else None}]}
                  for delta in deltas]
        no_choices: list[object] = []
        events.append(chunk | {"choices": no_choices, "usage": USAGE})
        data = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        self.send(handler, 200, data.encode(), "text/event-stream")

    @staticmethod
    def send(handler: BaseHTTPRequestHandler, status: int, data: bytes, content_type: str) -> None:
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            _ = handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


@unittest.skipUnless(os.path.isfile(KIMI_MAIN) and os.path.isfile(WIRE_NODE_ADDON),
                     "needs the built Kimi Code bundle and wirecap addon (pixi install)")
class KimiCaptureTests(unittest.TestCase):
    def test_print_mode_turn_is_captured_through_the_patch(self) -> None:
        model = LoopbackModel()
        self.addCleanup(model.close)
        with tempfile.TemporaryDirectory(prefix="tasksolver-kimi-capture-") as directory:
            root = Path(directory)
            for name in ("home", "workspace"):
                (root / name).mkdir()
            capture = root / "capture.jsonl"
            response = pykimi.ask(
                PROMPT, model=MODEL, workspace=str(root / "workspace"), timeout=120,
                kimi_home=str(root / "home"), capture=str(capture),
                extra_env={"KIMI_MODEL_PROVIDER_TYPE": "openai", "KIMI_MODEL_BASE_URL": model.url,
                           "KIMI_MODEL_API_KEY": "loopback", "KIMI_DISABLE_TELEMETRY": "1"})
            records = [json_loads_object(line) for line in capture.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
        detail = (f"exit {response.exit_status}, timed out {response.timed_out}, fixture saw {model.paths}, "
                  f"transcript tail {response.transcript[-1500:]!r}")
        self.assertFalse(response.timed_out, detail)
        self.assertEqual(response.exit_status, 0, detail)
        self.assertTrue(any(PROMPT in json.dumps(request.get("messages")) for request in model.requests), detail)

        # Each hook of the patch: the request, the wire journal, and the streamed events (as the
        # turn the worker decodes when the finish event arrives).
        kinds = collections.Counter(record.get("kind") for record in records)
        for kind in ("kimi_request", "kimi_wire", "kimi_turn"):
            self.assertGreater(kinds[kind], 0, f"no {kind} record: {dict(kinds)}; {detail}")
        turns = [record for record in records if record.get("kind") == "kimi_turn"
                 and json_object(record.get("request") or {}).get("first_user_text") == PROMPT]
        self.assertEqual(len(turns), 1, f"turns for the prompt: {len(turns)}; {dict(kinds)}")
        turn = turns[0]
        request = json_object(turn.get("request"))
        self.assertEqual((request.get("model"), request.get("protocol")), (MODEL, "openai"))
        self.assertEqual(turn.get("text"), ANSWER)
        self.assertTrue(turn.get("finish_reason"), turn)
        usage = json_object(turn.get("usage"))
        self.assertEqual((usage.get("input_tokens"), usage.get("output_tokens")), (21, 6))
        events = [json_object(event) for event in json_array(turn.get("events"))]
        types = [event.get("type") for event in events]
        self.assertEqual(types[-1], "finish", types)
        self.assertEqual(events[-1].get("rawFinishReason"), "stop", events[-1])
        # The streamed parts in the shape the decoder falls back on when no finish message arrives.
        parts = [json_object(event.get("part")) for event in events if event.get("type") == "part"]
        streamed = "".join(text for part in parts if part.get("type") == "text"
                           and isinstance(text := part.get("text"), str))
        self.assertEqual(streamed, ANSWER, parts)
        self.assertEqual(response.text, ANSWER)


if __name__ == "__main__":
    unittest.main()
