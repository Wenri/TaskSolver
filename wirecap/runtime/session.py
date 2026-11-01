"""Persistent-session lifecycle and turn collection for the linked CLIs."""

from __future__ import annotations

import multiprocessing as _mp
import time
from contextlib import ExitStack
from collections.abc import Callable, Mapping, Sequence
from multiprocessing.connection import Connection
from types import TracebackType
from typing import Literal, Protocol, Self, cast

from ..types import Event

from ..decode.mp_child import DONE as _DONE, EXC as _EXC

_SPAWN = _mp.get_context("spawn")

__all__ = ["ResultChannel", "TurnProcess", "SessionProcess", "TurnReason",
           "new_channel", "close_channel", "ask_turn", "WireSession"]


type TurnReason = Literal["turn", "idle", "deadline", "exit"]


class ResultChannel(Protocol):
    _reader: Connection
    _writer: Connection
    def get(self) -> object: ...
    def put(self, obj: object) -> None: ...


class TurnProcess(Protocol):
    @property
    def last_output(self) -> float: ...
    @last_output.setter
    def last_output(self, ts: float) -> None: ...
    def service_pty(self, timeout: float, readers: Sequence[Connection | int]) -> bool: ...
    def write(self, data: bytes) -> None: ...
    def submit(self, prompt: str) -> None: ...
    def reap(self) -> object: ...


class SessionProcess(TurnProcess, Protocol):
    _args: tuple[object, ...]
    @property
    def exit_status(self) -> int | None: ...
    @property
    def transcript(self) -> str: ...
    def start(self) -> None: ...
    def close(self, *, interrupt: bool = False, grace: float = 5.0) -> None: ...


def new_channel() -> ResultChannel:
    """A spawn-context result SimpleQueue for one CLI run. The caller keeps the
    reader; the child (the CLI) inherits both ends across execve and drops the
    reader. Close the parent's writer right after ``proc.start()`` so the
    reader EOFs when the CLI dies (crash detection)."""
    return cast(ResultChannel, _SPAWN.SimpleQueue())


def close_channel(q: ResultChannel) -> None:
    """Tear down the queue's pipe ends (idempotent). Its named SemLocks unlink
    via their own resource_tracker Finalize when ``q`` is GC'd; this just makes
    the fd close prompt."""
    for c in (q._reader, q._writer):
        try:
            c.close()
        except Exception:
            pass


def ask_turn(proc: TurnProcess, q: ResultChannel, prompt: str | None = None, *,
             kinds: Sequence[str], idle: float, pty_idle: float, timeout: float,
             ready: float = 2.5, pty_quiet: float = 0.0,
             pending: Callable[[list[Event]], bool] | None = None,
             event_kinds: Sequence[str] = ()) -> tuple[list[Event], TurnReason]:
    """Submit a prompt and collect decoded objects until the turn settles.

    Reasons are ``turn`` (answer events quiet for ``idle``), ``idle`` (no
    answer and PTY quiet for ``pty_idle``), ``deadline`` or ``exit``. The
    ``pending`` predicate and ``pty_quiet`` delay answer settling while tools
    are active. Side ``event_kinds`` are retained without resetting timers.
    ``ready`` lets the TUI settle before typing; ``prompt=None`` submits its
    argv prefill with a bare CR.
    """
    reader = q._reader
    rstart = time.time()
    while time.time() - rstart < 30 and time.time() - proc.last_output < ready:
        proc.service_pty(0.2, [reader])
    if prompt is None:
        proc.write(b"\r")                # submit the prefilled initial prompt
    else:
        proc.submit(prompt)              # type + submit a follow-up
    proc.last_output = time.time()       # measure idle from the submit, not the prior turn

    got: list[Event] = []
    last: float | None = None
    start = time.time()
    reason: TurnReason = "deadline"
    while time.time() - start < timeout:
        if proc.service_pty(0.2, [reader]):
            while reader.poll(0):
                try:
                    o = q.get()
                except EOFError:
                    proc.reap()          # the CLI exited — nothing more will ever arrive
                    return got, "exit"
                if isinstance(o, tuple) and o and cast(tuple[object, ...], o)[0] in (_DONE, _EXC):
                    proc.reap()          # the in-CLI target finished/raised: the stream is
                    return got, "exit"   # over — end the turn, don't wait out pty_idle
                if isinstance(o, dict):
                    event = cast(Event, o)
                    kind = event.get("kind")
                    if kind in kinds:
                        got.append(event)
                        last = time.time()
                    elif kind in event_kinds:
                        got.append(event)
        now = time.time()
        if last is not None and now - last >= idle:
            answers = [o for o in got if o.get("kind") in kinds]
            if pending is not None and pending(answers):
                continue                 # decoded turn still owes tool results — not the end
            if now - proc.last_output < pty_quiet:
                continue                 # the CLI is still painting — not the end either
            reason = "turn"              # turn(s) settled
            break
        if last is None and now - proc.last_output >= pty_idle:
            reason = "idle"              # the CLI went idle without producing a turn
            break
    return got, reason


class WireSession[ResponseT, ProcessT: SessionProcess = SessionProcess]:
    """Base for a multi-turn session over one live linked CLI.

    Providers implement ``_make_process`` (with child ``max_wait=None``),
    ``_latch_id`` and ``_build_response``. Optional startup/teardown hooks
    manage provider resources; failed startup and ``close`` unwind them once.
    Closed sessions cannot restart, but their ID and history remain readable.

    ``_KINDS``/``_EVENT_KINDS`` select answers and side events. Timing defaults
    ``_IDLE``, ``_PTY_IDLE``, ``_READY`` and ``_PTY_QUIET`` feed :func:`ask_turn`;
    ``_pending`` prevents settling during tool calls. ``_PREFILL_FIRST`` puts
    the first prompt on argv instead of typing it. ``_ID_KW`` names the native
    resume argument; ``conversation_id`` remains an alias for ``session_id``.

    Responses use streamed objects by default. ``capture_tail=True`` instead
    uses each provider's ``_load_capture_tail`` cursor to read only newly
    appended records, falling back to streamed objects when the tail is empty.
    """

    _KINDS: tuple[str, ...] = ()
    _EVENT_KINDS: tuple[str, ...] = ()
    _IDLE = 8.0
    _PTY_IDLE = 25.0
    _READY = 2.5
    _PTY_QUIET = 0.0
    _PREFILL_FIRST = True
    _ID_KW = "session_id"

    # shared instance state (providers set the rest in their __init__)
    _proc: ProcessT | None = None
    _q: ResultChannel | None = None
    _id: str | None = None
    _closed = False
    _ended = False
    idle: float
    timeout: float
    capture_tail = False

    # -- provider methods ----------------------------------------------------------------
    def _make_process(self, prompt: str | None) -> ProcessT:
        raise NotImplementedError

    def _latch_id(self) -> str | None:
        raise NotImplementedError

    def _build_response(self, objs: list[Event], reason: TurnReason) -> ResponseT:
        raise NotImplementedError

    def _read_history(self) -> list[Mapping[str, object]]:
        return []

    def _pre_start(self) -> None:
        pass

    def _post_start(self) -> None:
        pass

    def _teardown(self) -> None:
        pass

    def _pending(self, objs: list[Event]) -> bool:
        return False

    def _load_capture_tail(self) -> list[Event]:
        raise NotImplementedError(
            f"{type(self).__name__} does not support capture_tail")

    # -- lifecycle -------------------------------------------------------------
    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    def _start(self, prompt: str | None) -> None:
        try:
            self._pre_start()
            self._q = new_channel()
            self._proc = self._make_process(prompt)
            # Think-time between turns is unbounded; the child must have no deadline.
            args = self._proc._args
            assert args and len(args) >= 3 and args[2] is None, \
                "WireSession processes must be built with max_wait=None"
            self._proc.start()
            self._q._writer.close()   # parent reads only; reader EOFs on CLI death
            self._post_start()
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"Session cleanup also failed: {cleanup_error!r}")
            raise

    def ask(self, prompt: str) -> ResponseT:
        """Send ``prompt`` (starting the session on the first call) and return
        the provider Response for the turn it produced."""
        if self._closed:
            raise RuntimeError(
                f"this Session is closed; open a new Session "
                f"(resume with {self._ID_KW}={self._id!r})")
        if self._proc is not None:
            self._proc.reap()  # Detect a CLI that died while the caller was between turns.
        if self._proc is not None and (self._ended or self._proc.exit_status is not None):
            # The CLI died on an earlier turn (ask_turn reaped it). Every later
            # turn would silently return the stale transcript, so fail loudly —
            # resume in a fresh Session instead.
            raise RuntimeError(
                f"this Session's CLI process already exited "
                f"(exit_status={self._proc.exit_status}); open a new Session "
                f"(resume with {self._ID_KW}={self._id!r})")
        if self._proc is None:
            self._start(prompt if self._PREFILL_FIRST else None)
            first = None if self._PREFILL_FIRST else prompt
            objs, reason = self._turn(first)
        else:
            objs, reason = self._turn(prompt)
        self._ended = reason == "exit"
        if self.capture_tail:
            objs = self._load_capture_tail() or objs
        if self._id is None:
            self._id = self._latch_id()
        return self._build_response(objs, reason)

    def _turn(self, prompt: str | None) -> tuple[list[Event], TurnReason]:
        assert self._proc is not None and self._q is not None
        return ask_turn(self._proc, self._q, prompt, kinds=self._KINDS,
                        idle=self.idle, pty_idle=self._PTY_IDLE,
                        timeout=self.timeout, ready=self._READY,
                        pty_quiet=self._PTY_QUIET, pending=self._pending,
                        event_kinds=self._EVENT_KINDS)

    @property
    def session_id(self) -> str | None:
        """The CLI's native session/conversation id — the resumed id, or the
        one latched after the first turn. Persist it to resume later."""
        return self._id

    #: Cross-provider alias (agy spells it ``conversation_id``).
    conversation_id = session_id

    def history(self) -> list[Mapping[str, object]]:
        """The stored transcript, read from the CLI's own store; empty until
        an id is known."""
        if not self._id:
            return []
        return self._read_history()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        queue, self._q = self._q, None
        with ExitStack() as cleanup:
            cleanup.callback(self._teardown)
            if queue is not None:
                cleanup.callback(close_channel, queue)
            if self._proc is not None:
                self._proc.close(interrupt=True)
