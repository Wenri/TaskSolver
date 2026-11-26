#!/usr/bin/env python3
"""Kimi Code's capture hooks end to end, against a loopback model.

The built bundle and its wirecap addon run one print-mode turn through ``pykimi.ask``. The model
is an OpenAI-compatible chat-completions fixture on 127.0.0.1, defined by the ``KIMI_MODEL_*``
variables, with the store scoped by ``kimi_home``. The capture must hold what TaskSolver's wiretap
patch emits: the request with the prompt, wire-journal records, and the turn pykimi decodes from
the streamed events. A hook that an adoption dropped or misplaced fails here, and so does a stream
event the wiretap no longer recognizes (Kimi 2.8.0 renamed them); ``--version`` checks never send
a request. A two-turn ``pykimi.Session`` then drives the shell UI the same way, so a change to how
Kimi's editor takes typed input fails here too. No credentials and no network beyond loopback.

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
# The shell-mode turns: a single line (typed out with its CR, Kimi's editor took it for a pasted
# newline), then two lines over 1,000 characters (the editor shows such a paste as a marker
# and expands it on submit).
SHELL_FIRST: Final = "Shell turn one: remember the word osprey."
SHELL_SECOND: Final = "Shell turn two, its first line.\n" + " ".join(f"word{i}" for i in range(200))


def loopback_env(model: LoopbackModel) -> dict[str, str]:
    """The ``KIMI_MODEL_*`` family that points Kimi Code at the fixture."""
    return {"KIMI_MODEL_PROVIDER_TYPE": "openai", "KIMI_MODEL_BASE_URL": model.url,
            "KIMI_MODEL_API_KEY": "loopback", "KIMI_DISABLE_TELEMETRY": "1"}


def prompts_of(request: JSONObject) -> list[str]:
    """The user messages of a chat-completions request, without Kimi's ``<system-reminder>``s."""
    texts: list[str] = []
    for message in json_array(request.get("messages") or []):
        entry = json_object(message)
        if entry.get("role") != "user":
            continue
        content = entry.get("content")
        text = content if isinstance(content, str) else "".join(
            part_text for part in json_array(content or [])
            if isinstance(part_text := json_object(part).get("text"), str))
        if not text.lstrip().startswith("<system-reminder>"):
            texts.append(text)
    return texts


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
                kimi_home=str(root / "home"), capture=str(capture), extra_env=loopback_env(model))
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

    def test_shell_mode_session_submits_each_turn(self) -> None:
        """``pykimi.Session`` types each turn into the real TUI: each is submitted as its own
        request in one session, the second carrying the first, and each comes back as a
        captured ``kimi_turn``."""
        model = LoopbackModel()
        self.addCleanup(model.close)
        with tempfile.TemporaryDirectory(prefix="tasksolver-kimi-shell-") as directory:
            root = Path(directory)
            for name in ("home", "workspace"):
                (root / name).mkdir()
            with pykimi.Session(model=MODEL, workspace=str(root / "workspace"), timeout=120, idle=2,
                                kimi_home=str(root / "home"), extra_env=loopback_env(model)) as session:
                first = session.ask(SHELL_FIRST)
                second = session.ask(SHELL_SECOND)
                session_id = session.session_id
                history = [row.get("content") for row in session.history() if row.get("role") == "user"
                           and not str(row.get("content")).lstrip().startswith("<system-reminder>")]
        detail = f"fixture saw {model.paths}, transcript tail {second.transcript[-1500:]!r}"
        for response in (first, second):
            self.assertFalse(response.timed_out, detail)
            self.assertEqual(response.text, ANSWER, detail)
        self.assertEqual([prompts_of(request) for request in model.requests],
                         [[SHELL_FIRST], [SHELL_FIRST, SHELL_SECOND]], detail)
        self.assertTrue(session_id, detail)
        self.assertEqual(history, [SHELL_FIRST, SHELL_SECOND])
        records = [json_loads_object(json.dumps(turn)) for turn in (*first.turns, *second.turns)]
        captured = [json_object(record.get("request") or {}).get("first_user_text")
                    for record in records if record.get("kind") == "kimi_turn"]
        self.assertEqual(captured, [SHELL_FIRST, SHELL_SECOND])


if __name__ == "__main__":
    unittest.main()
