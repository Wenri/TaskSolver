"""Offline checks for a screen's web page: the terminal beside its live capture."""
from __future__ import annotations

import http.client
import json
import shutil
import sys
import unittest
from pathlib import Path

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect
from websockets.typing import Origin, Subprotocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_webterm import receive
from wirecap.runtime.terminal import TerminalManager
from wirecap.runtime.webpage import WebScreen
from wirecap.types import JSONObject, JSONValue, json_array, json_loads_object, json_object

_ECHO = ("import sys\nprint('WEB_READY', flush=True)\n"
         "for line in sys.stdin:\n print('REPLY:' + line.strip(), flush=True)\n")


class WebScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("tmux") or not shutil.which("ttyd"):
            raise RuntimeError("these native web-page checks require tmux and ttyd on PATH")

    def setUp(self) -> None:
        self.manager = TerminalManager()
        self.addCleanup(self.manager.close)

    def page(self, name: str, *, capture: bool, writable: bool = True) -> WebScreen:
        path = self.manager.directory / f"{name}.jsonl" if capture else None
        if path is not None:
            _ = path.write_text("")
        _ = self.manager.open(name, [sys.executable, "-u", "-c", _ECHO], capture_path=path)
        page = WebScreen(self.manager, screen=name, port=0, writable=writable)
        self.addCleanup(page.close)
        return page

    @staticmethod
    def get(page: WebScreen, path: str, host: str | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", page.port, timeout=5)
        try:
            connection.request("GET", path, headers={"Host": host} if host else {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_one_port_serves_the_page_and_a_typing_terminal(self) -> None:
        page = self.page("echo", capture=True)
        status, body = self.get(page, "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<iframe src="terminal/"', body)
        self.assertIn(b'const NAME = "echo";', body)
        self.assertEqual(self.get(page, "/terminal/")[0], 200)
        self.assertEqual(self.get(page, "/elsewhere")[0], 404)
        address = page.url.replace("http:", "ws:") + "/terminal/ws"
        with connect(address, origin=Origin(page.url), subprotocols=[Subprotocol("tty")],
                     close_timeout=1) as browser:
            browser.send(json.dumps({"AuthToken": "", "columns": 100, "rows": 30}))
            _ = receive(browser, b"WEB_READY")
            browser.send(b"0typed-in-page\r")
            _ = receive(browser, b"REPLY:typed-in-page")
        with self.assertRaises((WebSocketException, EOFError, OSError)):
            connect(address, origin=Origin("http://other.example"),
                    subprotocols=[Subprotocol("tty")], close_timeout=1).close()

    def test_capture_frames_follow_the_growing_file(self) -> None:
        page = self.page("codex", capture=True)
        path = self.manager.screens["codex"].capture_path
        assert path is not None

        def trips(since: int) -> tuple[JSONValue, list[JSONValue]]:
            listing = json_loads_object(self.get(page, f"/capture/trips?since={since}")[1])
            self.assertEqual(listing["capture"], str(path))
            return listing["rev"], json_array(listing["trips"])

        self.assertEqual(trips(0), (0, []))
        turn: JSONObject = {
            "kind": "codex_turn", "t": 12.5, "text": "h\u00e9llo\n answer", "reasoning": "why",
            "model": "m1", "finish_reason": "stop", "usage": {"total_tokens": 42},
            "request": {"first_user_text": "hi </script>"}, "events": [1, 2]}
        with path.open("a") as stream:
            _ = stream.write(json.dumps({"kind": "tls_write", "t": 1.0}) + "\nnot json\n")
            _ = stream.write(json.dumps(turn, ensure_ascii=False) + "\n")
            _ = stream.write('{"kind": "codex_turn", "text": "still being writ')
        self.assertEqual(trips(0), (1, [{
            "index": 0, "kind": "codex_turn", "rev": 1, "t": 12.5, "title": "m1", "tokens": 42,
            "up": "hi </script>", "down": "h\u00e9llo answer"}]))
        self.assertEqual(json_loads_object(self.get(page, "/capture/trip/0")[1]), turn)
        self.assertEqual(self.get(page, "/capture/trip/1")[0], 404)
        with path.open("a") as stream:
            _ = stream.write('ten"}\n')
        revision, later = trips(1)
        self.assertEqual((revision, [json_object(trip)["index"] for trip in later]), (2, [1]))
        self.assertEqual(json_loads_object(self.get(page, "/capture/trip/1")[1])["text"],
                         "still being written")
        self.assertEqual(trips(2), (2, []))
        self.assertEqual(self.get(page, "/capture/trips?since=x")[0], 400)

    def test_http_exchanges_pair_and_hide_credentials(self) -> None:
        page = self.page("agy", capture=True)
        path = self.manager.screens["agy"].capture_path
        assert path is not None

        def message(direction: str, sid: int, headers: JSONObject, body: bytes, t: float) -> str:
            return json.dumps({"kind": "h2msg", "conn": 7, "dir": direction, "h2sid": sid,
                               "headers": headers, "body_len": len(body),
                               "body_head": body[:256].hex(), "t": t}) + "\n"

        with path.open("a") as stream:
            _ = stream.write(message("c2s", 1, {
                ":method": "POST", ":authority": "api.example", ":path": "/v1/models",
                "authorization": "Bearer secret-token", "content-type": "application/json"}, b'{"a": 1}', 1.0))
            _ = stream.write(message("c2s", 3, {
                ":method": "POST", ":authority": "auth.example", ":path": "/token",
                "content-type": "application/x-www-form-urlencoded"}, b"refresh_token=secret-refresh", 2.0))
        listing = json_loads_object(self.get(page, "/capture/trips?since=0")[1])
        self.assertEqual(listing["trips"], [
            {"index": 0, "kind": "http", "rev": 1, "t": 1.0, "title": "POST",
             "up": "api.example/v1/models \u00b7 8 bytes", "down": None},
            {"index": 1, "kind": "http", "rev": 2, "t": 2.0, "title": "POST",
             "up": "auth.example/token \u00b7 28 bytes", "down": None}])
        with path.open("a") as stream:
            _ = stream.write(message("s2c", 1, {":status": "200", "set-cookie": "session=secret-cookie"},
                                     b'{"models": []}', 1.5))
        listing = json_loads_object(self.get(page, "/capture/trips?since=2")[1])
        self.assertEqual(listing["trips"], [
            {"index": 0, "kind": "http", "rev": 3, "t": 1.0, "title": "POST",
             "up": "api.example/v1/models \u00b7 8 bytes", "down": "200 \u00b7 14 bytes"}])
        status, body = self.get(page, "/capture/trip/0")
        self.assertEqual(status, 200)
        content = json_loads_object(body)
        self.assertEqual((content["method"], content["host"], content["path"], content["status"]),
                         ("POST", "api.example", "/v1/models", "200"))
        request, response = json_object(content["request"]), json_object(content["response"])
        self.assertEqual(json_object(request["headers"])["authorization"], "(hidden)")
        self.assertEqual(request["body_head"], '{"a": 1}')
        self.assertEqual(json_object(response["headers"])["set-cookie"], "(hidden)")
        self.assertEqual(response["body_head"], '{"models": []}')
        everything = body + self.get(page, "/capture/trip/1")[1] + self.get(page, "/capture/trips")[1]
        self.assertNotIn(b"secret-", everything)

    def test_page_is_loopback_only_and_a_screen_without_capture_says_so(self) -> None:
        page = self.page("claude", capture=False, writable=False)
        self.assertEqual(json_loads_object(self.get(page, "/capture/trips")[1]),
                         {"capture": None, "rev": 0, "trips": []})
        self.assertEqual(self.get(page, "/capture/trips", host="rebound.example")[0], 403)
        self.assertEqual(self.get(page, "/", host="localhost:1")[0], 200)

    def test_page_closes_with_its_screen_and_frees_the_port(self) -> None:
        page = self.page("echo", capture=True)
        other = self.page("other", capture=False)
        with self.assertRaises(OSError):
            _ = WebScreen(self.manager, screen="other", port=page.port)
        self.manager.screens["echo"].close()
        self.assertFalse(page.alive)
        with self.assertRaises(OSError):
            _ = self.get(page, "/")
        self.assertTrue(other.alive)
        self.assertEqual(self.get(other, "/")[0], 200)
        with self.assertRaises(KeyError):
            _ = WebScreen(self.manager, screen="echo", port=0)


if __name__ == "__main__":
    _ = unittest.main()
