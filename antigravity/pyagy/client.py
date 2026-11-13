"""Public agy client: decoded responses, MCP tools/context, and system notifications.

Responses prefer a complete app snapshot, then decoded wire text, then the PTY transcript.
Capture errors remain available through ``response_capture_errors``. ``stack`` and
``arg_probe`` enable native diagnostics. All launches use the linked host via AgyProcess.

The public rewrite argument is retained for compatibility but currently raises
NotImplementedError: the native TLS trampoline only captures outbound bytes.
"""
from __future__ import annotations

import os
import json
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import cached_property
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, Unpack, cast

from . import config as _config
from . import conversations as _conv
from ._term import answer_text as _answer_text
from ._pty import service_many as _service_many
from .agyprocess import AgyProcess
from .conversations import ensure_git_workspace
from .types import (ContextResource as ContextResource, RewriteRule as RewriteRule, ToolSpec as ToolSpec,
                    ContextInput, ToolInput, RewriteFunction, RewriteSpec)
from wirecap.decode.mp_child import DONE as _DONE, EXC as _EXC   # result-queue completion sentinels
from wirecap.decode.turns import Usage as Usage, primary_turn, sum_usage
from wirecap.runtime import session as _wire_session
from wirecap.runtime.session import WireSession as _WireSession, ResultChannel, TurnReason
from wirecap.runtime.mcp import MCPServers
from wirecap.types import Event, JSONObject, JSONValue, json_loads_object, json_object

if TYPE_CHECKING:
    from .agy_process.symbolize import Symbolizer
    from .system_messages import SystemMessageChannel

type RewriteInput = str | RewriteFunction | Sequence[RewriteRule | RewriteSpec]


class ClientOptions(TypedDict, total=False):
    model: str | None
    workspace: str | None
    tools: Sequence[ToolSpec | ToolInput] | None
    context: Sequence[ContextResource | ContextInput] | None
    mcp_servers: MCPServers | None
    rewrite: RewriteInput | None
    capture: bool | str
    timeout: float
    skip_permissions: bool
    agy_bin: str | None
    extra_env: Mapping[str, str | None] | None
    stack: bool
    arg_probe: bool
    funcmap: str | None
    data_dir: str | None
    app_data_dir: str | None
    account: str | None
    isolated_auth: bool | None
    trust: bool
    extra_flags: Sequence[str] | None
    system_messages: Iterable[object] | str | None


class AskOptions(ClientOptions, total=False):
    conversation_id: str | None
    continue_latest: bool


class SessionDefaults(ClientOptions, total=False):
    idle: float
    capture_tail: bool
    enable_system_messages: bool


class SessionOptions(SessionDefaults, total=False):
    conversation_id: str | None
    continue_latest: bool


class ResponseMeta(TypedDict):
    exit_status: int | None
    capture_path: str | None
    workspace: str
    linked: NotRequired[bool]
    linked_reason: NotRequired[str]
    funcmap: NotRequired[str | None]
    conversation_id: NotRequired[str | None]


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""

_UNIQ = [0]
_ANSWER_KINDS = ("genai_turn", "app_response")   # decoded objects the default target streams home
_EVENT_KINDS = ("system_message", "system_message_capture_error", "response_capture_error")
_STREAM_KINDS = _ANSWER_KINDS + _EVENT_KINDS
__all__ = ["AgyResponse", "Session", "ask", "ask_many", "resume", "continue_latest",
           "ToolSpec", "ContextResource", "RewriteRule", "Usage", "_ask_turn", "_load_capture"]


# --- response ----------------------------------------------------------------
@dataclass
class AgyResponse:
    text: str
    transcript: str
    turns: list[Event]
    exit_status: int | None
    capture_path: str | None
    workspace: str
    linked: bool
    linked_reason: str = ""
    app_turns: list[str] = field(default_factory=list[str])   # app-boundary answers (fh_update)
    funcmap: str | None = None                              # symbols/funcmap.tsv.gz for .stacks
    conversation_id: str | None = None                      # agy's native conversation id (resumable)
    system_messages: list[Event] = field(default_factory=list[Event])
    # Each record is one observed serialization of a native system-message step.
    # History can be serialized repeatedly; retain those observations and their metadata.
    system_message_errors: list[Event] = field(default_factory=list[Event])
    response_capture_errors: list[Event] = field(default_factory=list[Event])

    def __str__(self) -> str:
        return self.text

    @classmethod
    def from_objs(cls, objs: Sequence[Event], transcript: str, **meta: Unpack[ResponseMeta]) -> AgyResponse:
        """Build a response from the decoded objects the worker streamed home + the PTY
        transcript. One answer policy: the app-boundary text (longest ``app_response``), else
        the wire turn (the primary ``genai_turn`` with text — the most tokens, since the
        session-title call's streamed thoughts can outrun a short answer), else the filtered
        transcript — so ``.text`` and ``.source`` always agree. ``**meta`` supplies
        exit_status/capture_path/workspace/funcmap/conversation_id (``linked`` defaults
        True)."""
        turns = [o for o in objs if o.get("kind") == "genai_turn"]
        app_texts = [text for o in objs if o.get("kind") == "app_response"
                     and isinstance(text := o.get("text"), str) and text]
        response_capture_errors = [o for o in objs if o.get("kind") == "response_capture_error"]
        if any(o.get("capture_source") == "app_response" for o in response_capture_errors):
            app_texts = []  # A later incomplete snapshot can supersede an earlier short answer.
        wire = primary_turn([o for o in turns if o.get("text")])
        text = (max(app_texts, key=len) if app_texts else
                _text(wire["text"]) if wire else _answer_text(transcript))
        meta.setdefault("linked", True)
        system_messages = [o for o in objs if o.get("kind") == "system_message"]
        system_message_errors = [o for o in objs if o.get("kind") == "system_message_capture_error"]
        return cls(text=text, transcript=transcript, turns=turns, app_turns=app_texts,
                   system_messages=system_messages, system_message_errors=system_message_errors,
                   response_capture_errors=response_capture_errors,
                   **meta)

    @property
    def app_text(self) -> str:
        """The assembled answer captured at agy's own consumer boundary
        (``updateWithStep``'s step argument) — the app-boundary RESPONSE. ``""``
        when the capture holds no ``app_response`` events. The text-bearing fires carry
        the full answer, so take the longest."""
        return max(self.app_turns, key=len) if self.app_turns else ""

    @property
    def source(self) -> Literal["app", "wire", "transcript"]:
        """Where ``.text`` came from: ``"app"`` (app-boundary decode, preferred),
        ``"wire"`` (http1sse genai_turn), or ``"transcript"`` (PTY fallback). Matches the
        answer policy in :meth:`from_objs`."""
        if self.app_turns:
            return "app"
        if any(t.get("text") for t in self.turns):
            return "wire"
        return "transcript"

    @property
    def primary(self) -> Event | None:
        """The substantive model turn (most tokens) — agent turns dwarf the
        title-generation calls. None if nothing was decoded."""
        return primary_turn(self.turns)

    @property
    def request(self) -> Mapping[str, object] | None:
        p = self.primary
        request = p.get("request") if p else None
        return cast(Mapping[str, object], request) if isinstance(request, Mapping) else None

    @property
    def events(self) -> list[JSONObject]:
        p = self.primary
        events: object = p.get("events") if p else None
        return cast(list[JSONObject], events) if isinstance(events, list) else []

    @property
    def json(self) -> Event | None:
        """The full decoded primary turn (request summary + response text/usage)."""
        return self.primary

    @property
    def model(self) -> str | None:
        """The requested model id (from the wire request), falling back to the served
        ``modelVersion`` decoded from the response — so a request too large for the shim's
        per-fire capture cap still yields a model id off the response side."""
        p = self.primary
        if not p:
            return None
        request = self.request
        model = (request.get("model") if request else None) or p.get("model")
        return model if isinstance(model, str) else None

    @property
    def usage(self) -> Usage:
        return sum_usage(self.turns)

    # --- diagnostic decoders (present only when the matching capture exists) ---
    # These lazily import their agy_process module inside the accessor so a plain
    # `import pyagy` / `ask()` never loads the RPC/stack/graph decoders (or the
    # 132k-row funcmap) unless you actually read the attribute. Each degrades to a
    # falsy/None value when its events (or the funcmap) aren't in this run's capture.
    def _has_capture(self) -> bool:
        return bool(self.capture_path and self.linked
                    and os.path.exists(self.capture_path))

    @cached_property
    def rpc_trace(self) -> str:
        """A time-ordered, labeled RPC timeline from the ``CodeAssistClient`` books —
        ``StreamGenerateContent`` is the model turn. ``""`` when the capture holds no
        ``rpc_*`` events."""
        if not self._has_capture():
            return ""
        from .agy_process import rpctrace
        assert self.capture_path is not None
        return rpctrace.trace(self.capture_path)

    @cached_property
    def cgt_args(self) -> list[str]:
        """The trampoline arg-graph reports captured with ``arg_probe=True``
        (``AGY_PROC_CGT_ARGS``) — one rendered string per book fire. ``[]`` otherwise."""
        if not self._has_capture():
            return []
        reports: list[str] = []
        assert self.capture_path is not None
        with open(self.capture_path) as f:
            for line in f:
                if '"cgt_args"' not in line:
                    continue
                try:
                    o = json_loads_object(line)
                except ValueError:
                    continue
                report = o.get("report")
                if o.get("kind") == "cgt_args" and isinstance(report, str) and report:
                    reports.append(report)
        return reports

    @cached_property
    def _symbolizer(self) -> Symbolizer | None:
        """A funcmap-backed Symbolizer, or None if the funcmap file is absent
        (``pixi run shim-symbols`` produces it; it's gitignored)."""
        from .agy_process import symbolize
        path = self.funcmap or symbolize.DEFAULT_FUNCMAP
        if not os.path.exists(path):
            return None
        return symbolize.Symbolizer(path)

    @cached_property
    def stacks(self) -> str | None:
        """Symbolized, grouped call stacks captured with ``stack=True``
        (``AGY_PROC_STACK``) — a rendered string. ``None`` when there's no capture;
        a short reason string when the funcmap is missing."""
        if not self._has_capture():
            return None
        sym = self._symbolizer
        if sym is None:
            from .agy_process import symbolize
            return (f"(funcmap not found at {self.funcmap or symbolize.DEFAULT_FUNCMAP}; "
                    "run `pixi run shim-symbols`)")
        from .agy_process import symbolize
        assert self.capture_path is not None
        return symbolize.render_stacks(self.capture_path, sym)

    @cached_property
    def call_graph(self) -> Counter[tuple[str, str]] | None:
        """Caller→callee edge counts from the ``stack=True`` capture — a
        ``collections.Counter`` keyed by ``(caller, callee)``. ``None`` without a
        capture or funcmap."""
        if not self._has_capture():
            return None
        sym = self._symbolizer
        if sym is None:
            return None
        from .agy_process import symbolize
        assert self.capture_path is not None
        return symbolize.call_graph(self.capture_path, sym)


# --- helpers -----------------------------------------------------------------
def _check_rewrite(spec: RewriteInput | None) -> None:
    if spec is not None:
        raise NotImplementedError("agy request rewriting is unsupported by the current native trampoline; "
                                  "use tools/context or system_messages to supply additional input")


def _prepare_capture(capture: bool | str, workspace: str, filename: str,
                     index: int | None = None) -> str | None:
    if not capture:
        return None
    path = capture if isinstance(capture, str) else os.path.join(workspace, filename)
    if index is not None:
        stem, extension = os.path.splitext(path)
        path = f"{stem}-{index}{extension}"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w"):
        pass
    return path


def _load_capture_objs(capture_path: str | None, since: int = 0) -> tuple[list[Event], int]:
    """Return decoded answer and side-event records after a line cursor.

    Keep the original records, including system-message provenance and truncation
    fields. A single cursor covers all kinds in a cumulative Session capture.
    """
    objs: list[Event] = []
    n = since
    if not capture_path or not os.path.exists(capture_path):
        return objs, since
    with open(capture_path) as f:
        for n, line in enumerate(f, 1):
            if n <= since:
                continue
            line = line.strip()
            if not line or not any('"' + kind + '"' in line for kind in _STREAM_KINDS):
                continue
            try:
                obj: Event = dict(json_loads_object(line))
            except (TypeError, ValueError):
                continue
            if obj.get("kind") in _STREAM_KINDS:
                objs.append(obj)
    return objs, n


def _load_capture(capture_path: str | None, since: int = 0) -> tuple[list[Event], list[str], int]:
    """Historical ``(genai_turns, app_texts, cursor)`` capture reader."""
    objs, cursor = _load_capture_objs(capture_path, since)
    return ([o for o in objs if o["kind"] == "genai_turn"],
            [text for o in objs if o["kind"] == "app_response"
             and isinstance(text := o.get("text"), str) and text],
            cursor)


def _capture_or_stream(objs: Sequence[Event], capture_path: str | None) -> list[Event]:
    """An exiting CLI's capture is authoritative for each recorded kind.

    Preserve streamed kinds absent from the capture: capture can be disabled, or
    a partial recording may contain a system notification but no model answer.
    """
    recorded, _ = _load_capture_objs(capture_path)
    kinds = {kind for o in recorded if isinstance(kind := o.get("kind"), str)}
    return recorded + [o for o in objs if o.get("kind") not in kinds]


def _message_texts(messages: Iterable[object] | str | None) -> tuple[str, ...]:
    """Validate once before parallel launches; a string is one message, not characters."""
    if messages is None:
        return ()
    if isinstance(messages, str):
        messages = (messages,)
    messages = tuple(messages)
    if any(not isinstance(text, str) or not text.strip() for text in messages):
        raise ValueError("system_messages must contain nonempty strings")
    return tuple(text for text in messages if isinstance(text, str))


def _message_channel(workspace: str, conversation_id: str | None = None) -> SystemMessageChannel:
    from .system_messages import SystemMessageChannel
    return SystemMessageChannel(workspace, conversation_id=conversation_id)


def _shim_overlays(rewrite: RewriteInput | None, stack: bool, arg_probe: bool,
                   extra_env: Mapping[str, str | None] | None) -> dict[str, str | None]:
    """Validate supported controls and prepare native diagnostics."""
    _check_rewrite(rewrite)
    env = dict(extra_env or {})
    if stack:
        env["AGY_PROC_STACK"] = "1"
    if arg_probe:
        env["AGY_PROC_CGT_ARGS"] = "1"
    return env


def _server_name() -> str:
    _UNIQ[0] += 1
    return f"pyagy-{os.getpid()}-{_UNIQ[0]}"


@dataclass(eq=False)
class _MCPLayer:
    value: JSONValue


@dataclass
class _MCPState:
    original: JSONValue
    existed: bool
    layers: list[_MCPLayer] = field(default_factory=list[_MCPLayer])


_MCP_LOCK = threading.RLock()
_MCP_OVERLAYS: dict[str, dict[str, _MCPState]] = {}


def _temporary_mcp_servers(servers: MCPServers) -> Callable[[], None]:
    """Stack temporary global entries so overlapping sessions can close in either order."""
    path = os.path.realpath(_config.detect_config_path())
    normalized = {name: _config.normalize_server_spec(spec) for name, spec in servers.items()}
    installed = json_loads_object(json.dumps(normalized))
    bindings: dict[str, tuple[_MCPState, _MCPLayer]] = {}
    with _MCP_LOCK:
        try:
            with open(path) as stream:
                before = json_loads_object(stream.read() or "{}")
        except FileNotFoundError:
            before = {}
        previous = json_object(before.get("mcpServers", {}))
        _config.write_mcp_servers(normalized, path=path, merge=True)
        states = _MCP_OVERLAYS.setdefault(path, {})
        for name, value in installed.items():
            state = states.get(name)
            if state is None or previous.get(name) != state.layers[-1].value:
                state = states[name] = _MCPState(previous.get(name), name in previous)
            layer = _MCPLayer(value)
            state.layers.append(layer)
            bindings[name] = state, layer
    closed = False

    def restore() -> None:
        nonlocal closed
        with _MCP_LOCK:
            if closed:
                return
            closed = True
            current: JSONObject | None = None
            entries: JSONObject = {}
            try:
                with open(path) as stream:
                    current = json_loads_object(stream.read() or "{}")
                entries = json_object(current.get("mcpServers", {}))
            except (FileNotFoundError, TypeError, ValueError):
                current = None  # Preserve a deletion or malformed edit made during the run.
            states = _MCP_OVERLAYS.get(path, {})
            changed = False
            for name, (state, layer) in bindings.items():
                top = state.layers[-1] is layer
                state.layers.remove(layer)
                if states.get(name) is not state or not top:
                    continue
                if current is not None and entries.get(name) == layer.value:
                    if state.layers:
                        entries[name] = state.layers[-1].value
                    elif state.existed:
                        entries[name] = state.original
                    else:
                        del entries[name]
                    changed = True
                if not state.layers:
                    del states[name]
            if not states:
                _MCP_OVERLAYS.pop(path, None)
            if not changed:
                return
            temporary = ""
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False) as stream:
                    temporary = stream.name
                    json.dump(current, stream, indent=2)
                os.replace(temporary, path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)

    return restore


def _inject_config(tools: Sequence[ToolSpec | ToolInput] | None,
                   context: Sequence[ContextResource | ContextInput] | None,
                   mcp_servers: MCPServers | None = None, data_dir: str | None = None,
                   app_data_dir: str | None = None, account: str | None = None,
                   isolated_auth: bool | None = None) -> Callable[[], None]:
    """Provision agy's MCP config for one run/session. Returns a cleanup callable.

    ``tools``/``context`` register TaskSolver's own spec-backed server under a
    generated name, removed by cleanup. All servers use the same scoped config
    when ``data_dir`` is supplied.

    ``mcp_servers`` registers arbitrary ``{name: {command, args, env}}`` servers
    (see :func:`pyagy.config.write_mcp_servers`; every command gets the
    PYTHONHOME unwrap). With ``data_dir`` the servers go into that scoped home's
    own ``config/mcp_config.json`` — the store is created first with
    ``link_global_config=False`` so the run neither observes nor mutates the
    user's global servers, and the config persists for resume/inspection (no
    cleanup). Without ``data_dir`` they merge into the GLOBAL store and the
    cleanup restores previous entries still owned by this run. In-process overlays can
    close in either order; separate processes should use ``data_dir`` to avoid races.
    """
    path = None
    if data_dir and (tools or context or mcp_servers):
        _conv.prepare_scoped_home(
            data_dir, app_data_dir=app_data_dir, link_global_config=False,
            isolated_auth=isolated_auth, account=account)
        path = os.path.join(os.path.abspath(data_dir), ".gemini", "config", "mcp_config.json")
    with _MCP_LOCK, ExitStack() as cleanup:
        if tools or context:
            name = _server_name()
            _config.write_mcp_config(tools=tools, context=context, server_name=name, path=path)
            cleanup.callback(_config.remove_mcp_config, name, path)
        if mcp_servers:
            if path is None:
                cleanup.callback(_temporary_mcp_servers(mcp_servers))
            else:
                _config.write_mcp_servers(mcp_servers, path=path, merge=True)
        registered = cleanup.pop_all()

    def close() -> None:
        with _MCP_LOCK:
            registered.close()
    return close


# --- caller-owned result channel (the queue + drain helpers) -----------------
# Stock-mp layering: we create the SimpleQueue, hand it to AgyProcess via ``args=(q,)`` (so it rides
# the process pickle), and drain it here — AgyProcess is just the Process. ``proc.service_pty`` keeps
# agy's PTY drained in the same wait we use to read the queue, so no background thread is needed.
def _new_channel() -> ResultChannel:
    """A spawn-context result SimpleQueue for one agy run (hoisted to
    :func:`wirecap.runtime.session.new_channel`; kept as the historical name)."""
    return _wire_session.new_channel()


def _close_channel(q: ResultChannel) -> None:
    """Tear down the queue's pipe ends (idempotent; hoisted to
    :func:`wirecap.runtime.session.close_channel`)."""
    _wire_session.close_channel(q)


def _collect(proc: AgyProcess, q: ResultChannel, timeout: float = 300.0,
             kinds: Sequence[str] = _STREAM_KINDS) -> list[Event]:
    """One-shot: drain the PTY and collect the decoded objects (of ``kinds``) the target streams home
    over ``q`` until agy exits / the target signals done (``_wire_done``/``_wire_exc``) / ``timeout``.
    Returns the dicts in arrival order (possibly empty; use ``proc.transcript`` as the fallback)."""
    reader = q._reader
    got: list[Event] = []
    start = time.time()
    while time.time() - start < timeout:
        if proc.service_pty(1.0, [reader]):        # drains the PTY; True when the queue is readable
            try:
                while reader.poll(0):
                    o = q.get()
                    if isinstance(o, tuple) and o and o[0] in (_DONE, _EXC):
                        proc.reap()                 # reap agy so exit_status is set
                        return got
                    if isinstance(o, dict):
                        event = cast(Event, o)
                        if event.get("kind") in kinds:
                            got.append(event)
            except EOFError:
                proc.reap()                         # agy exited — the normal one-shot completion
                return got
    return got


def _ask_turn(proc: AgyProcess, q: ResultChannel, prompt: str | None = None, idle: float = 6.0,
              pty_idle: float = 15.0, timeout: float = 180.0, ready: float = 2.5,
              kinds: Sequence[str] = _ANSWER_KINDS) -> list[Event]:
    """Persistent multi-turn: submit ``prompt`` (or the ``--prompt-interactive`` prefill if None),
    then collect the decoded objects (of ``kinds``) for that turn from ``q`` until it settles.

    Compat shim over :func:`wirecap.runtime.session.ask_turn`, keeping this module's historical
    defaults and objs-only return (the hoisted version also reports *why* the turn ended)."""
    got, _reason = _wire_session.ask_turn(proc, q, prompt, kinds=kinds, idle=idle,
                                          pty_idle=pty_idle, timeout=timeout, ready=ready,
                                          event_kinds=_EVENT_KINDS)
    return got


def _collect_many(procs: Sequence[AgyProcess], queues: Sequence[ResultChannel],
                  timeout: float = 300.0, kinds: Sequence[str] = _STREAM_KINDS) -> list[list[Event]]:
    """One-shot collect from several already-``start()``ed AgyProcesses concurrently, in one event
    loop. ``procs`` and ``queues`` are parallel. A single ``_pty.service_many`` watches every live
    proc's PTY + queue reader together, so all PTYs are drained while each proc's ``kinds`` objects
    are gathered until it signals done or its queue EOFs. Returns a list parallel to ``procs`` — each
    entry that proc's dicts in arrival order."""
    got: list[list[Event]] = [[] for _ in procs]
    done = [False] * len(procs)
    readers = [q._reader for q in queues]
    reader_idx = {readers[i]: i for i in range(len(procs))}
    end = time.time() + timeout
    while not all(done) and time.time() < end:
        live = [i for i in range(len(procs)) if not done[i]]
        if not live:
            break
        pops = [procs[i]._wire_popen() for i in live]         # PTY-multiplex is a _pty-layer primitive
        rds = [readers[i] for i in live]
        for r in _service_many(pops, rds, max(0.0, end - time.time())):
            i = reader_idx[r]
            try:
                while r.poll(0):
                    o = queues[i].get()
                    if isinstance(o, tuple) and o and o[0] in (_DONE, _EXC):
                        procs[i].reap()               # reap agy so exit_status is set
                        done[i] = True
                        break
                    if isinstance(o, dict):
                        event = cast(Event, o)
                        if event.get("kind") in kinds:
                            got[i].append(event)
            except EOFError:
                procs[i].reap()                       # agy exited — the normal completion signal
                done[i] = True
    return got


# --- one-shot ----------------------------------------------------------------
def ask(prompt: str, *, model: str | None = None, workspace: str | None = None,
        tools: Sequence[ToolSpec | ToolInput] | None = None,
        context: Sequence[ContextResource | ContextInput] | None = None,
        mcp_servers: MCPServers | None = None, rewrite: RewriteInput | None = None,
        capture: bool | str = True, timeout: float = 300, skip_permissions: bool = False,
        agy_bin: str | None = None, extra_env: Mapping[str, str | None] | None = None,
        stack: bool = False, arg_probe: bool = False, funcmap: str | None = None,
        conversation_id: str | None = None, continue_latest: bool = False,
        data_dir: str | None = None, app_data_dir: str | None = None,
        account: str | None = None, isolated_auth: bool | None = None,
        trust: bool = True, extra_flags: Sequence[str] | None = None,
        system_messages: Iterable[object] | str | None = ()) -> AgyResponse:
    """Run one ``agy --print`` turn and return a decoded :class:`AgyResponse`.

    The shim installs the full working book union, so one turn populates the wire
    (``.turns``/``.request``/``.usage``/``.model``), app-boundary (``.app_text``), and
    RPC-trace (``.rpc_trace``) surfaces together. The overlays ``stack=True`` /
    ``arg_probe=True`` add the diagnostic call-stack / trampoline arg-graph, surfaced on
    ``.stacks``/``.call_graph`` and ``.cgt_args``. ``funcmap`` overrides the symbol map
    used by ``.stacks``.

    To run with seeded ``AGENTS.md`` instructions or skills, seed a workspace with
    ``ensure_git_workspace(...)`` and pass it as ``workspace=`` — omitting it resolves the
    shared scratch repo with no seeds, which *clears* any a previous call left there.

    ``conversation_id`` resumes a stored conversation (``--conversation=<id>``, works in
    print mode) and ``continue_latest`` resumes the most recent one (``--continue``); the
    resulting :attr:`AgyResponse.conversation_id` is the id this run created/continued, so
    a later ``ask(..., conversation_id=r.conversation_id)`` continues it.

    ``app_data_dir`` selects the subdirectory under ``~/.gemini/`` (default ``"pyagy"`` via
    ``agy --app_data_dir=pyagy``, sharing global ``~/.gemini/config`` and login while keeping
    programmatic conversations separate from interactive ``~/.gemini/antigravity-cli``).
    ``data_dir`` scopes agy's whole ``.gemini`` tree under that directory (a project repo)
    instead of the global ``~/.gemini`` — login is preserved by seeding credentials (see
    :func:`pyagy.conversations.prepare_scoped_home`). ``trust`` (default on) pre-registers the
    workspace in the target store's ``trustedWorkspaces`` so interactive mode never blocks on the
    folder-trust prompt.

    ``system_messages`` queues strings as native harness system-message steps before
    the next model invocation. These retain system attribution; they are not a
    replacement for the provider's system prompt. Observations, including messages
    generated by agy itself, appear in ``response.system_messages``.
    """
    messages = _message_texts(system_messages)
    workspace = ensure_git_workspace(workspace)
    cap_path = _prepare_capture(capture, workspace, "pyagy-capture.jsonl")
    overlays = _shim_overlays(rewrite, stack, arg_probe, extra_env)
    overlays["AGY_PROC_LOG"] = os.path.join(workspace, "pyagy-shim.log")  # shim logs off the PTY

    cleanup = _inject_config(tools, context, mcp_servers, data_dir, app_data_dir,
                             account=account, isolated_auth=isolated_auth)
    q = _new_channel()                              # caller owns the result queue (stock-mp style)
    p = None
    channel = None
    try:
        if messages:
            channel = _message_channel(workspace, conversation_id)
            for text in messages:
                channel.send(text)
            overlays.update(channel.start())
        p = AgyProcess(prompt=prompt, model=model, skip_permissions=skip_permissions,
                       agy_bin=agy_bin, workdir=workspace, capture=cap_path or os.devnull,
                       args=(q, _STREAM_KINDS, timeout + 60),  # max_wait > timeout → death-based done
                       conversation_id=conversation_id, continue_latest=continue_latest,
                       data_dir=data_dir, app_data_dir=app_data_dir,
                       account=account, isolated_auth=isolated_auth, trust=trust,
                       extra_env=overlays, extra_flags=extra_flags)
        p.start()
        q._writer.close()                           # parent reads only; reader now EOFs on agy death
        objs = _collect(p, q, timeout=timeout)      # decoded answer streamed home over the queue
        transcript = p.transcript                   # raw PTY (fallback / diagnostics)
        cid = p.conversation_id
    finally:
        # close() in the finally so a KeyboardInterrupt / mid-collect raise never leaves agy running
        # (it is idempotent). It also SIGTERMs + blocking-reaps, so exit_status is only reliably set
        # afterwards — on the _collect timeout path nothing had reaped yet.
        try:
            if p is not None:
                p.close()
        finally:
            try:
                _close_channel(q)
                cleanup()
            finally:
                if channel is not None:
                    channel.close()
    exit_status = p.exit_status if p is not None else None

    return AgyResponse.from_objs(
        _capture_or_stream(objs, cap_path), transcript,
        exit_status=exit_status, capture_path=cap_path,
        workspace=workspace, funcmap=funcmap, conversation_id=cid)


def ask_many(prompt: str, n: int, *, model: str | None = None, workspace: str | None = None,
        tools: Sequence[ToolSpec | ToolInput] | None = None,
        context: Sequence[ContextResource | ContextInput] | None = None,
        mcp_servers: MCPServers | None = None, rewrite: RewriteInput | None = None,
        capture: bool | str = True, timeout: float = 300, skip_permissions: bool = False,
        agy_bin: str | None = None, extra_env: Mapping[str, str | None] | None = None,
        stack: bool = False, arg_probe: bool = False, funcmap: str | None = None,
        conversation_id: str | None = None, continue_latest: bool = False,
        data_dir: str | None = None, app_data_dir: str | None = None,
        account: str | None = None, isolated_auth: bool | None = None,
        trust: bool = True, extra_flags: Sequence[str] | None = None,
        system_messages: Iterable[object] | str | None = ()) -> list[AgyResponse]:
    """Run ``n`` independent ``agy --print`` turns of the same ``prompt`` concurrently and
    return a list of ``n`` decoded :class:`AgyResponse` (parallel sampling — e.g. ``AgyModel``
    with ``n_choices>1``). Same kwargs as :func:`ask`; all share one workspace + tools/context,
    each gets its own capture file. No threads: every process is ``start()``ed (a fast, serial,
    non-blocking fork), then the caller-side ``_collect_many`` services them all in one event loop
    (draining every PTY + result queue together). ``n <= 1`` delegates to :func:`ask`."""
    messages = _message_texts(system_messages)
    if n <= 1:
        return [ask(prompt, model=model, workspace=workspace, tools=tools, context=context,
                    mcp_servers=mcp_servers, rewrite=rewrite, capture=capture, timeout=timeout,
                    skip_permissions=skip_permissions, agy_bin=agy_bin, extra_env=extra_env,
                    stack=stack, arg_probe=arg_probe, funcmap=funcmap,
                    conversation_id=conversation_id, continue_latest=continue_latest,
                    data_dir=data_dir, app_data_dir=app_data_dir,
                    account=account, isolated_auth=isolated_auth, trust=trust,
                    extra_flags=extra_flags, system_messages=messages)]
    workspace = ensure_git_workspace(workspace)
    overlays = _shim_overlays(rewrite, stack, arg_probe, extra_env)
    overlays["AGY_PROC_LOG"] = os.path.join(workspace, "pyagy-shim.log")
    cap_paths = [_prepare_capture(capture, workspace, "pyagy-capture.jsonl", i)
                 for i in range(n)]
    cleanup = _inject_config(tools, context, mcp_servers, data_dir, app_data_dir,
                             account=account, isolated_auth=isolated_auth)  # one shared MCP config for all n
    queues = [_new_channel() for _ in range(n)]   # one caller-owned result queue per proc
    procs: list[AgyProcess] = []
    channels: list[SystemMessageChannel] = []
    try:
        for i in range(n):
            process_env = dict(overlays)
            if messages:
                channel = _message_channel(workspace, conversation_id)
                channels.append(channel)
                for text in messages:
                    channel.send(text)
                process_env.update(channel.start())
            procs.append(AgyProcess(prompt=prompt, model=model, skip_permissions=skip_permissions,
                            agy_bin=agy_bin, workdir=workspace, capture=cap_paths[i] or os.devnull,
                            args=(queues[i], _STREAM_KINDS, timeout + 60),  # > timeout → death-based
                            conversation_id=conversation_id, continue_latest=continue_latest,
                            data_dir=data_dir, app_data_dir=app_data_dir,
                            account=account, isolated_auth=isolated_auth, trust=trust,
                            extra_env=process_env, extra_flags=extra_flags))
        for p, q in zip(procs, queues):
            p.start()                            # non-blocking fork; serial → no fork-in-thread
            q._writer.close()                    # parent reads only; reader EOFs on that agy's death
        objs_list = _collect_many(procs, queues, timeout=timeout)   # one event loop services all n
        # Flush the final captured events and settle exit status before building responses.
        for p in procs:
            p.close()
        responses = [AgyResponse.from_objs(
                         _capture_or_stream(objs, cap), p.transcript,
                         exit_status=p.exit_status, capture_path=cap,
                         workspace=workspace, funcmap=funcmap, conversation_id=p.conversation_id)
                     for p, objs, cap in zip(procs, objs_list, cap_paths)]
        return responses
    finally:
        # close in the finally (idempotent) so a raise mid-start/mid-collect never leaves any agy
        # running — the old placement also skipped every already-started proc if start() raised.
        # ExitStack attempts every cleanup even if a process or edited hook config raises.
        with ExitStack() as closing:
            for channel in channels:
                closing.callback(channel.close)
            closing.callback(cleanup)
            for q in queues:
                closing.callback(_close_channel, q)
            for p in procs:
                closing.callback(p.close)


# --- multi-turn --------------------------------------------------------------
class Session(_WireSession[AgyResponse, AgyProcess]):
    """A multi-turn agy session — **the first-class object of pyagy**. Same kwargs as
    :func:`ask`; ``ask(prompt)`` starts it on first call and continues it thereafter.

    In-run turns ride one live ``agy --prompt-interactive`` process. Across a restart,
    agy's *native* conversation store keeps context: pass ``conversation_id=`` (resume a
    specific stored conversation) or ``continue_latest=True`` (resume the most recent),
    or use the module helpers :func:`resume` / :func:`continue_latest`. After the first
    turn, :attr:`conversation_id` holds this session's id — persist it to resume later,
    and read :meth:`history` for the stored transcript.

    ``rewrite`` and ``set_rewrite`` reject unsupported outbound rewriting.
    ``send_system_message(text, conversation_id=...)`` queues a native system notification
    for the main agent (default) or a specific subagent's next model invocation. Injection
    can be disabled with ``enable_system_messages=False``; capture remains enabled.
    ``system_messages=[...]`` seeds initial notifications and ``system_message_ids`` contains
    their queue IDs. ``system_message_status(id)`` reports emission separately from capture.
    Use the Session as a context manager to guarantee cleanup. Lifecycle (lazy start,
    dead-process guard,
    channel plumbing, turn loop) lives on :class:`wirecap.runtime.session.WireSession`;
    this class supplies agy's process construction and response shape."""

    _KINDS = _ANSWER_KINDS
    _EVENT_KINDS = _EVENT_KINDS
    _IDLE = 25.0
    _PTY_IDLE = 15.0
    _ID_KW = "conversation_id"

    def __init__(self, *, model: str | None = None, workspace: str | None = None,
                 tools: Sequence[ToolSpec | ToolInput] | None = None,
                 context: Sequence[ContextResource | ContextInput] | None = None,
                 mcp_servers: MCPServers | None = None, rewrite: RewriteInput | None = None,
                 capture: bool | str = True, timeout: float = 180, idle: float = 25.0,
                 agy_bin: str | None = None, extra_env: Mapping[str, str | None] | None = None,
                 stack: bool = False, arg_probe: bool = False, funcmap: str | None = None,
                 conversation_id: str | None = None, continue_latest: bool = False,
                 skip_permissions: bool = False, data_dir: str | None = None,
                 app_data_dir: str | None = None, account: str | None = None,
                 isolated_auth: bool | None = None, trust: bool = True,
                 extra_flags: Sequence[str] | None = None, capture_tail: bool = False,
                 system_messages: Iterable[object] | str | None = (),
                 enable_system_messages: bool = True) -> None:
        _check_rewrite(rewrite)
        messages = _message_texts(system_messages)
        if messages and not enable_system_messages:
            raise ValueError("system-message injection is disabled for this Session")
        self.workspace = ensure_git_workspace(workspace)
        self.agy_bin = agy_bin
        self.model = model
        self.timeout = timeout
        self.idle = idle
        self.capture = capture
        self.capture_tail = capture_tail
        self.stack = stack
        self.arg_probe = arg_probe
        self.funcmap = funcmap
        self.extra_env = extra_env
        self.extra_flags = extra_flags
        self.rewrite = rewrite
        self._tools = tools
        self._context = context
        self._mcp_servers = mcp_servers
        self.continue_latest = continue_latest      # start with --continue (most recent)
        self.skip_permissions = skip_permissions
        self._data_dir = data_dir                    # scope the conversation store to a repo
        self._app_data_dir = app_data_dir            # store subdir under ~/.gemini (default: pyagy)
        self._account = account                      # named pyagy account profile
        self._isolated_auth = isolated_auth          # dedicated file-backed auth vs shared global login
        self._trust = trust                          # pre-trust the workspace (no folder prompt)
        self._home: _conv.PathInput | None = None                            # resolved scoped home (None = global store)
        self._id = conversation_id                   # resume this id; else captured after turn 1
        self.cap_path: str | None = None
        self._cap_cursor = 0                         # capture_tail line cursor (one per session)
        self._cleanup: Callable[[], None] = lambda: None
        self._closed = False
        self._messages = (_message_channel(self.workspace, conversation_id)
                          if enable_system_messages else None)
        try:
            self.system_message_ids = ([self._messages.send(text) for text in messages]
                                       if self._messages is not None else [])
        except BaseException:
            if self._messages is not None:
                self._messages.close()
            raise

    def _pre_start(self) -> None:
        self.cap_path = _prepare_capture(self.capture, self.workspace, "pyagy-session.jsonl")
        self._overlays = _shim_overlays(self.rewrite, self.stack, self.arg_probe, self.extra_env)
        self._overlays["AGY_PROC_LOG"] = os.path.join(self.workspace, "pyagy-session.log")
        self._cleanup = _inject_config(self._tools, self._context,
                                       self._mcp_servers, self._data_dir, self._app_data_dir,
                                       account=self._account, isolated_auth=self._isolated_auth)
        if self._messages is not None:
            self._overlays.update(self._messages.start())

    def _make_process(self, prompt: str | None) -> AgyProcess:
        return AgyProcess(persistent=True, prompt=prompt, model=self.model,
                          skip_permissions=self.skip_permissions, agy_bin=self.agy_bin,
                          workdir=self.workspace, capture=self.cap_path or os.devnull,
                          args=(self._q, _STREAM_KINDS, None),  # max_wait=None: base asserts
                          conversation_id=self._id,
                          continue_latest=self.continue_latest,
                          data_dir=self._data_dir, app_data_dir=self._app_data_dir,
                          account=self._account, isolated_auth=self._isolated_auth,
                          trust=self._trust,
                          extra_env=self._overlays, extra_flags=self.extra_flags)

    def _post_start(self) -> None:
        assert self._proc is not None
        self._home = self._proc.home                 # scoped store home (for .history())
        self._app_data_dir = self._proc.app_data_dir # resolved store subdir (for .history())

    def _latch_id(self) -> str | None:
        assert self._proc is not None
        cid = self._proc.conversation_id
        if cid and self._messages is not None:
            self._messages.bind_main(cid)
        return cid

    def _build_response(self, objs: list[Event], reason: TurnReason) -> AgyResponse:
        assert self._proc is not None
        return AgyResponse.from_objs(
            objs, self._proc.transcript, exit_status=self._proc.exit_status,
            capture_path=self.cap_path, workspace=self.workspace, funcmap=self.funcmap,
            conversation_id=self._id)

    def _read_history(self) -> list[Mapping[str, object]]:
        """The stored transcript for this conversation, read from agy's own store — a list
        of ``{step_index, role, type, status, created_at, content}`` (see
        :func:`pyagy.conversations.read_transcript`)."""
        return (list(_conv.read_transcript(self._id, app_data_dir=self._app_data_dir, home=self._home))
                if self._id else [])

    def _load_capture_tail(self) -> list[Event]:
        """This turn's records from the cumulative session capture: the lines appended
        since the previous turn's cursor, re-shaped into stream-object dicts."""
        objs, self._cap_cursor = _load_capture_objs(self.cap_path, since=self._cap_cursor)
        return objs

    def _teardown(self) -> None:
        self._closed = True
        try:
            self._cleanup()
        finally:
            if self._messages is not None:
                self._messages.close()

    def send_system_message(self, text: object, *, conversation_id: str | None = None) -> str:
        """Queue a native system notification for the next model invocation.

        The default recipient is this Session's main agent. Pass a subagent's
        conversation ID to target it. May be called before the first ``ask`` or
        from another thread while ``ask`` is running. This does not wake an idle
        agent; the message waits until its next native PreInvocation hook.
        Returns an ID for :meth:`system_message_status`.
        """
        if self._closed:
            raise RuntimeError("cannot queue a system message on a closed Session")
        if self._messages is None:
            raise RuntimeError("system-message injection is disabled for this Session")
        return self._messages.send(text, conversation_id=conversation_id)

    def system_message_status(self, message_id: object) -> JSONObject:
        """Queue status; ``emitted`` means hook output, not confirmed model delivery."""
        if self._messages is None:
            raise RuntimeError("system-message injection is disabled for this Session")
        return self._messages.status(message_id)

    def set_rewrite(self, spec: RewriteInput | None) -> Session:
        """Rewriting is unavailable with the current native trampoline."""
        _check_rewrite(spec)
        self.rewrite = None
        return self


# --- session entry points (Session is the first-class object of pyagy) -------
def resume(conversation_id: str, **kwargs: Unpack[SessionDefaults]) -> Session:
    """A :class:`Session` that resumes the stored conversation ``conversation_id``
    (``agy --conversation=<id>``). Its first ``.ask()`` continues that conversation with
    full prior context — even in a brand-new process. ``**kwargs`` are :class:`Session`'s."""
    return Session(conversation_id=conversation_id, **kwargs)


def continue_latest(**kwargs: Unpack[SessionDefaults]) -> Session:
    """A :class:`Session` resuming agy's most recent conversation (``agy --continue``)."""
    return Session(continue_latest=True, **kwargs)


# Read-only store helpers, re-exported so `pyagy.list_conversations()` / `.latest_...` work.
list_conversations = _conv.list_conversations
latest_conversation_id = _conv.latest_conversation_id
