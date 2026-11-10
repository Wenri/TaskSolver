"""A browser page for one managed screen: its terminal beside the live capture.

The page has three frames: the screen's ttyd terminal, the list of captured round trips
(the decoded ``*_turn`` events of the screen's capture JSONL) and the selected trip's
content. One loopback port serves all of it; the terminal is ttyd behind ``/terminal``.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Self, cast, override
from urllib.parse import parse_qs, urlsplit

from ..types import JSONObject, JSONValue, json_loads
from .terminal import TerminalManager
from .webterm import WebTerminal

__all__ = ["WebScreen"]

_TERMINAL = "/terminal"
_LOCAL_HOST = re.compile(r"(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", re.IGNORECASE)
# Headers that describe one connection, not the proxied response.
_HOP_HEADERS = frozenset(("connection", "keep-alive", "transfer-encoding", "content-length",
                          "upgrade", "proxy-connection", "te", "trailer"))


def _text(value: JSONValue, limit: int) -> str | None:
    return " ".join(value.split())[:limit] if isinstance(value, str) and value.strip() else None


# Shown as "(hidden)": the page is for reading traffic, not for copying credentials out of it.
_SECRET_HEADERS = frozenset(("authorization", "proxy-authorization", "cookie", "set-cookie",
                             "x-api-key", "x-goog-api-key", "api-key"))


def _http_side(event: JSONObject) -> JSONObject:
    """One captured HTTP message for display: no credentials, the body's captured head as text."""
    raw = event.get("headers")
    headers: JSONObject = {
        name: "(hidden)" if name.lower() in _SECRET_HEADERS else value
        for name, value in (raw.items() if isinstance(raw, dict) else ())}
    side: JSONObject = {"t": event.get("t"), "headers": headers, "body_len": event.get("body_len")}
    head = event.get("body_head")
    content_type = headers.get("content-type")
    if isinstance(content_type, str) and "x-www-form-urlencoded" in content_type:
        side["body"] = "(hidden: form bodies carry credentials)"
    elif isinstance(head, str) and head:
        try:
            side["body_head"] = bytes.fromhex(head).decode("utf-8", "replace")
        except ValueError:
            pass
    return side


class _Trip:
    def __init__(self, index: int, kind: str) -> None:
        self.summary: JSONObject = {"index": index, "kind": kind}
        self.lines: list[tuple[int, int]] = []  # (offset, length) of its events in the capture


class _Trips:
    """Index of the round trips in a capture file that is still being written.

    A trip is a decoded model turn (a ``*_turn`` event) or an HTTP/2 exchange (``h2msg``
    events; a response joins its request). ``since`` revisions let a page fetch only the
    trips that are new or have changed.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._offset = 0
        self._revision = 0
        self._trips: list[_Trip] = []
        self._open: dict[tuple[JSONValue, JSONValue], _Trip] = {}

    def _touch(self, trip: _Trip, line: tuple[int, int], **fields: JSONValue) -> None:
        self._revision += 1
        trip.lines.append(line)
        trip.summary.update(fields, rev=self._revision)

    def _new(self, kind: str) -> _Trip:
        trip = _Trip(len(self._trips), kind)
        self._trips.append(trip)
        return trip

    def _add(self, event: JSONObject, line: tuple[int, int]) -> None:
        kind = event.get("kind")
        if isinstance(kind, str) and kind.endswith("_turn"):
            usage = event.get("usage")
            request = event.get("request")
            self._touch(
                self._new(kind), line, t=event.get("t"), title=event.get("model"),
                tokens=usage.get("total_tokens") if isinstance(usage, dict) else None,
                up=_text(request.get("first_user_text"), 160) if isinstance(request, dict) else None,
                down=_text(event.get("text"), 160) or (
                    "(reasoning only)" if _text(event.get("reasoning"), 1) else
                    _text(event.get("finish_reason"), 40) or "(no text)"))
        elif kind == "h2msg":
            headers = event.get("headers")
            if not isinstance(headers, dict):
                return
            key = (event.get("conn"), event.get("h2sid"))
            size = f"{event.get('body_len') or 0} bytes"
            if event.get("dir") == "c2s":
                trip = self._open[key] = self._new("http")
                target = f"{headers.get(':authority') or ''}{headers.get(':path') or ''}"
                self._touch(trip, line, t=event.get("t"), title=headers.get(":method"),
                            up=f"{target[:140]} \u00b7 {size}", down=None)
            else:
                trip = self._open.pop(key, None) or self._new("http")
                self._touch(trip, line, t=trip.summary.get("t") or event.get("t"),
                            down=f"{headers.get(':status') or 'response'} \u00b7 {size}")

    def _refresh(self) -> None:
        if self.path is None:
            return
        try:
            with self.path.open("rb") as stream:
                _ = stream.seek(self._offset)
                data = stream.read()
        except OSError:
            return
        # Only complete lines: the recorder may be in the middle of the last one.
        end = data.rfind(b"\n") + 1
        position = self._offset
        self._offset += end
        for line in data[:end].splitlines(keepends=True):
            start, position = position, position + len(line)
            try:
                event = json_loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                self._add(event, (start, len(line)))

    def since(self, revision: int) -> tuple[int, list[JSONObject]]:
        with self._lock:
            self._refresh()
            return self._revision, [trip.summary for trip in self._trips
                                    if cast(int, trip.summary["rev"]) > revision]

    def content(self, index: int) -> JSONObject | None:
        with self._lock:
            self._refresh()
            if self.path is None or not 0 <= index < len(self._trips):
                return None
            trip = self._trips[index]
            events: list[JSONObject] = []
            try:
                with self.path.open("rb") as stream:
                    for start, length in trip.lines:
                        _ = stream.seek(start)
                        event = json_loads(stream.read(length))
                        if isinstance(event, dict):
                            events.append(event)
            except (OSError, ValueError):
                return None
        if trip.summary["kind"] != "http":
            return events[0] if events else None
        content: JSONObject = {"kind": "http"}
        for event in events:
            side = _http_side(event)
            headers = cast(JSONObject, side["headers"])
            if event.get("dir") == "c2s":
                content.update(request=side, req_t=event.get("t"), host=headers.get(":authority"),
                               method=headers.get(":method"), path=headers.get(":path"))
            else:
                content.update(response=side, t=event.get("t"), status=headers.get(":status"))
        return content


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, port: int, *, name: str, trips: _Trips, terminal_port: int) -> None:
        self.name = name
        self.trips = trips
        self.terminal_port = terminal_port
        super().__init__(("127.0.0.1", port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TaskSolver"
    sys_version = ""

    @override
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        _ = self.wfile.write(body)

    def _json(self, value: JSONValue, status: int = 200) -> None:
        self._send(status, "application/json", json.dumps(value, ensure_ascii=False).encode())

    def do_GET(self) -> None:
        server = cast(_Server, self.server)
        # A page of another site must not reach this server under a rebound name.
        if _LOCAL_HOST.fullmatch(self.headers.get("Host", "")) is None:
            self._json({"error": "this page is served on loopback only"}, 403)
            return
        url = urlsplit(self.path)
        if url.path == "/":
            self._send(200, "text/html; charset=utf-8", _page(server.name))
        elif url.path == "/capture/trips":
            try:
                revision = int(parse_qs(url.query).get("since", ["0"])[0])
            except ValueError:
                self._json({"error": "since must be an integer"}, 400)
                return
            path = server.trips.path
            latest, trips = server.trips.since(revision)
            self._json({"capture": None if path is None else str(path), "rev": latest,
                        "trips": cast(list[JSONValue], trips)})
        elif url.path.startswith("/capture/trip/"):
            index = url.path.removeprefix("/capture/trip/")
            content = server.trips.content(int(index)) if index.isdecimal() else None
            if content is None:
                self._json({"error": "no such round trip"}, 404)
            else:
                self._json(content)
        elif url.path == _TERMINAL or url.path.startswith(_TERMINAL + "/"):
            if "upgrade" in self.headers.get("Connection", "").lower():
                self._tunnel(server.terminal_port)
            else:
                self._fetch(server.terminal_port)
        else:
            self._json({"error": "not found"}, 404)

    def _fetch(self, port: int) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", self.path, headers={
                name: value for name, value in self.headers.items() if name.lower() not in _HOP_HEADERS})
            response = connection.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException):
            self._json({"error": "the terminal is not running"}, 502)
            return
        finally:
            connection.close()
        self.send_response(response.status)
        for name, value in response.getheaders():
            if name.lower() not in _HOP_HEADERS and name.lower() not in ("server", "date"):
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)

    def _tunnel(self, port: int) -> None:
        """Hand a WebSocket upgrade to ttyd unchanged (it checks the Origin against the Host)."""
        self.close_connection = True
        try:
            upstream = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            self._json({"error": "the terminal is not running"}, 502)
            return
        # The handler's own socket attribute is untyped; this is the same connection.
        with upstream, socket.socket(fileno=os.dup(self.rfile.fileno())) as client:
            upstream.settimeout(None)
            head = [self.requestline, *(f"{name}: {value}" for name, value in self.headers.items())]

            def forward() -> None:
                try:
                    upstream.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
                    while data := self.rfile.read1(65536):
                        upstream.sendall(data)
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        upstream.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            sender = threading.Thread(target=forward, daemon=True)
            sender.start()
            try:
                while data := upstream.recv(65536):
                    client.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    client.shutdown(socket.SHUT_RDWR)  # also ends the sender's read
                except OSError:
                    pass
                sender.join()


class WebScreen:
    """Serve one screen's page on 127.0.0.1: terminal, round-trip list and trip content.

    The capture frames read the screen's ``capture_path`` as it grows; a screen without
    capture says so. Typing is allowed only with ``writable=True``. The page
    closes with its screen or the manager; closing it leaves the screen running.
    """

    def __init__(self, manager: TerminalManager, *, screen: str, port: int = 7681,
                 writable: bool = False, ttyd_bin: str | None = None, timeout: float = 10.0) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        selected = manager.screens[screen]
        self.screen = screen
        self.writable = writable
        self._closed = False
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._terminal: WebTerminal | None = None
        try:
            self._terminal = WebTerminal(manager, port=0, writable=writable, ttyd_bin=ttyd_bin,
                                         screen=screen, timeout=timeout, base_path=_TERMINAL)
            self._server = _Server(port, name=screen, trips=_Trips(selected.capture_path),
                                   terminal_port=self._terminal.port)
            self.port = self._server.server_address[1]
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                            kwargs={"poll_interval": 0.1})
            self._thread.start()
            manager._register_cleanup(self.close, screen=selected)
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"Web page cleanup also failed: {cleanup_error!r}")
            raise

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def alive(self) -> bool:
        return not self._closed and self._terminal is not None and self._terminal.alive

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._server is not None:
                if self._thread is not None:
                    self._server.shutdown()
                    self._thread.join()
                self._server.server_close()
        finally:
            if self._terminal is not None:
                self._terminal.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()


def _page(name: str) -> bytes:
    # The name is data inside a script element: keep it from closing the element.
    return _PAGE.replace("__NAME__", json.dumps(name).replace("<", "\\u003c")).encode()


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TaskSolver</title>
<style>
  :root { color-scheme: dark; --bg: #16181d; --panel: #1d2027; --line: #2c313c; --text: #d7dae0;
          --dim: #8b93a3; --accent: #7aa2f7; --select: #2a3350; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { display: grid; grid-template-columns: minmax(0, 3fr) minmax(280px, 2fr);
         background: var(--bg); color: var(--text); font: 13px/1.45 system-ui, sans-serif; }
  iframe { width: 100%; height: 100%; border: 0; background: #000; }
  #side { display: grid; grid-template-rows: minmax(120px, 35%) minmax(0, 1fr); min-height: 0;
          border-left: 1px solid var(--line); }
  section { display: flex; flex-direction: column; min-height: 0; }
  section + section { border-top: 1px solid var(--line); }
  h2 { margin: 0; padding: 6px 10px; font-size: 11px; font-weight: 600; letter-spacing: .06em;
       text-transform: uppercase; color: var(--dim); background: var(--panel);
       border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; gap: 8px; }
  h2 span { text-transform: none; letter-spacing: 0; font-weight: 400; overflow: hidden; flex: 1;
            text-align: right;
            text-overflow: ellipsis; white-space: nowrap; }
  .scroll { overflow: auto; min-height: 0; flex: 1; }
  #trips { list-style: none; margin: 0; padding: 0; }
  #trips li { padding: 6px 10px; border-bottom: 1px solid var(--line); cursor: pointer; }
  #trips li:hover { background: var(--panel); }
  #trips li[aria-selected="true"] { background: var(--select); }
  .row { display: flex; gap: 8px; justify-content: space-between; color: var(--dim); font-size: 12px;
         font-variant-numeric: tabular-nums; }
  .row b { color: var(--accent); font-weight: 600; }
  #trips li.http .row b { color: var(--dim); }
  #trips.turns li.http { display: none; }
  h2 label { text-transform: none; letter-spacing: 0; font-weight: 400; white-space: nowrap; }
  h2 input { vertical-align: -2px; margin: 0 2px 0 0; }
  .preview { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty { padding: 14px 10px; color: var(--dim); }
  #content { padding: 0 10px 12px; }
  #content h3 { margin: 12px 0 4px; font-size: 12px; color: var(--accent); }
  pre { margin: 0; padding: 8px; background: var(--panel); border: 1px solid var(--line);
        border-radius: 4px; white-space: pre-wrap; overflow-wrap: anywhere;
        font: 12px/1.45 ui-monospace, monospace; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 2px 12px; margin: 10px 0 0; }
  dt { color: var(--dim); } dd { margin: 0; overflow-wrap: anywhere; }
  details { margin-top: 10px; } summary { cursor: pointer; color: var(--dim); }
  @media (max-width: 800px) { body { grid-template-columns: 1fr; grid-template-rows: 55% 45%; }
                               #side { border-left: 0; border-top: 1px solid var(--line); } }
</style>
</head>
<body>
<iframe src="terminal/" title="Terminal"></iframe>
<div id="side">
  <section>
    <h2>Round trips <span id="status"></span>
      <label><input type="checkbox" id="turns"> model turns only</label></h2>
    <div class="scroll"><ul id="trips"></ul><div class="empty" id="empty">Waiting for the first round trip.</div></div>
  </section>
  <section>
    <h2>Content <span id="which"></span></h2>
    <div class="scroll"><div id="content"><div class="empty">Select a round trip.</div></div></div>
  </section>
</div>
<script>
const NAME = __NAME__;
document.title = NAME + " — TaskSolver";
const list = document.getElementById("trips"), empty = document.getElementById("empty");
const status = document.getElementById("status"), content = document.getElementById("content");
const which = document.getElementById("which");
const items = new Map(), turnsOnly = document.getElementById("turns");
let revision = 0, last = -1, selected = -1;
turnsOnly.addEventListener("change", () => list.classList.toggle("turns", turnsOnly.checked));

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function clock(t) { return typeof t === "number" ? new Date(t * 1000).toLocaleTimeString() : ""; }
function pretty(value) { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }

function show(trip) {
  let item = items.get(trip.index);
  if (!item) {
    item = element("li");
    item.dataset.index = trip.index;
    item.addEventListener("click", () => select(trip.index));
    items.set(trip.index, item);
    list.append(item);
  }
  item.className = trip.kind === "http" ? "http" : "turn";
  const row = element("div", undefined, "row");
  const head = element("span");
  head.append(element("b", "#" + (trip.index + 1)), " " + (trip.title || trip.kind));
  row.append(head, element("span", [trip.tokens != null ? trip.tokens + " tok" : "", clock(trip.t)]
    .filter(Boolean).join(" \u00b7 ")));
  item.replaceChildren(row);
  if (trip.up) item.append(element("div", "\u2192 " + trip.up, "preview"));
  item.append(element("div", "\u2190 " + (trip.down || "\u2026"), "preview"));
}

async function select(index) {
  selected = index;
  for (const item of list.children) item.setAttribute("aria-selected", item.dataset.index == index);
  await load(index);
}

async function load(index) {
  which.textContent = "#" + (index + 1);
  let trip;
  try {
    const response = await fetch("capture/trip/" + index);
    if (!response.ok) throw new Error(response.statusText);
    trip = await response.json();
  } catch (error) {
    if (selected === index) content.replaceChildren(element("div", "Could not load this round trip: " + error.message, "empty"));
    return;
  }
  if (selected !== index) return;
  const facts = element("dl");
  for (const [label, value] of [["Kind", trip.kind], ["Model", trip.model],
       ["Method", trip.method], ["Host", trip.host], ["Path", trip.path], ["Status", trip.status],
       ["Requested", clock(trip.req_t)], ["Answered", clock(trip.t)], ["Finish", trip.finish_reason], ["Usage", trip.usage && JSON.stringify(trip.usage.raw || trip.usage)]]) {
    if (value) facts.append(element("dt", label), element("dd", String(value)));
  }
  const parts = [facts];
  const shown = new Set(["kind", "model", "t", "req_t", "host", "finish_reason", "usage",
                         "method", "path", "status"]);
  for (const [key, title] of [["request", "Request"], ["reasoning", "Reasoning"], ["text", "Response"],
                              ["response", "Response"]]) {
    shown.add(key);
    const value = trip[key];
    if (value == null || value === "" || (typeof value === "object" && !Object.keys(value).length)) continue;
    parts.push(element("h3", title), element("pre", pretty(value)));
  }
  for (const key of Object.keys(trip)) {
    if (shown.has(key)) continue;
    // Full requests and event streams are large: render them only when opened.
    const details = element("details");
    details.append(element("summary", key));
    details.addEventListener("toggle", () => {
      if (details.open && details.children.length === 1) details.append(element("pre", pretty(trip[key])));
    });
    parts.push(details);
  }
  content.replaceChildren(...parts);
  content.parentElement.scrollTop = 0;
}

async function poll() {
  try {
    const response = await fetch("capture/trips?since=" + revision);
    if (!response.ok) throw new Error(response.statusText);
    const data = await response.json();
    if (data.capture === null) {
      empty.textContent = "This screen has no native capture.";
      status.textContent = "";
    } else {
      status.textContent = data.capture;
      status.title = data.capture;
      const following = selected === last;
      for (const trip of data.trips) {
        show(trip);
        last = Math.max(last, trip.index);
      }
      revision = data.rev;
      if (items.size) empty.hidden = true;
      if (following && last !== selected) select(last);
      else if (data.trips.some(trip => trip.index === selected)) load(selected);
    }
  } catch (error) {
    status.textContent = "disconnected";
  }
  setTimeout(poll, 1000);
}
poll();
</script>
</body>
</html>
"""
