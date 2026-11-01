#!/usr/bin/env python3
"""Offline unit tests for wirecap.runtime.session — the shared persistent-turn
loop and the WireSession base. No CLI, no PTY, no network: `ask_turn` is driven
with a duck-typed fake process and a real spawn-context SimpleQueue, so every
settle path is deterministic.

Pins: the four `reason` values; `pty_quiet` suppressing an early settle while
the terminal still paints; `pending` suppressing the idle break while tool
results are owed (and `timeout` still ending such a turn); kind filtering;
lazy start; the dead-process guard; `close()` idempotence; and the
`max_wait=None` assertion that keeps a numeric deadline from sneaking back
into Session processes.

    python3 test_scripts/test_wire_session.py
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from multiprocessing.connection import Connection
from typing import Literal
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)

from wirecap.decode.mp_child import DONE, EXC  # noqa: E402
from wirecap.runtime.session import (  # noqa: E402
    ResultChannel,
    WireSession,
    ask_turn,
    close_channel,
    new_channel,
)
from wirecap.types import Event

_failures: list[str] = []


def check(cond: object, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


type ScriptAction = tuple[float, Literal["obj"], object] | tuple[float, Literal["paint", "die"]]
type TurnResult = tuple[list[Event], str]


class FakeProc:
    """Duck-type of the WirePtyProcess surface ask_turn touches. A `script` maps
    a time offset (s, from the submit) to an action: ("obj", dict) enqueues a
    decoded object, ("paint",) bumps last_output (the TUI repainted),
    ("die",) drops every writer end (EOF at the reader).

    Like the real child, the fake holds its OWN copy of the queue's writer (a
    dup'd fd) — WireSession closes the parent's writer right after start(), and
    the crash-detection EOF must still only fire when the "child" lets go."""

    def __init__(self, q: ResultChannel, script: Sequence[ScriptAction] = ()) -> None:
        import multiprocessing.connection as _mpc
        self.q = q
        self._w = _mpc.Connection(os.dup(q._writer.fileno()), readable=False)
        self.script = sorted(script, key=lambda e: e[0])
        self.last_output = time.time()
        self.exit_status: int | None = None
        self.reaped = False
        self.submitted: list[str] = []
        self._t0: float | None = None

    # -- surface ---------------------------------------------------------------
    def service_pty(self, timeout: float, readers: Sequence[Connection | int]) -> bool:
        # replay any due script actions, then report reader readability
        now = time.time()
        if self._t0 is not None:
            while self.script and now - self._t0 >= self.script[0][0]:
                action = self.script.pop(0)
                if action[1] == "obj":
                    self._w.send(action[2])   # what SimpleQueue.put does, sans lock
                elif action[1] == "paint":
                    self.last_output = time.time()
                elif action[1] == "die":
                    self._w.close()
                    try:
                        self.q._writer.close()
                    except Exception:
                        pass
        time.sleep(min(timeout, 0.02))
        return self.q._reader.poll(0)

    def write(self, data: bytes) -> None:
        self._arm()

    def submit(self, prompt: str) -> None:
        self.submitted.append(prompt)
        self.last_output = time.time()
        self._arm()

    def reap(self) -> bool:
        self.reaped = True
        self.exit_status = 0
        return True

    def _arm(self) -> None:
        self._t0 = time.time()


def _turn(script: Sequence[ScriptAction], *, kinds: Sequence[str] = ("t",),
          idle: float = 0.15, pty_idle: float = 0.3, timeout: float = 3.0,
          ready: float = 0.0, pty_quiet: float = 0.0,
          pending: Callable[[list[Event]], bool] | None = None):
    q = new_channel()
    proc = FakeProc(q, script)
    got, reason = ask_turn(proc, q, "go", kinds=kinds, idle=idle, pty_idle=pty_idle,
                           timeout=timeout, ready=ready, pty_quiet=pty_quiet, pending=pending)
    close_channel(q)
    proc._w.close()
    return got, reason, proc


def test_reasons() -> None:
    print("[offline] ask_turn: the four settle reasons")
    got, reason, _ = _turn([(0.02, "obj", {"kind": "t", "n": 1})])
    check(reason == "turn" and len(got) == 1, "decoded turn + quiet -> 'turn'")

    got, reason, _ = _turn([])  # nothing ever arrives, PTY quiet
    check(reason == "idle" and got == [], "no turn + PTY quiet -> 'idle'")

    got, reason, _ = _turn([(0.02, "obj", {"kind": "t"})],
                           idle=10.0, pty_idle=10.0, timeout=0.4)
    check(reason == "deadline" and len(got) == 1, "timeout mid-turn -> 'deadline'")

    got, reason, proc = _turn([(0.02, "obj", {"kind": "t"}), (0.05, "die")])
    check(reason == "exit" and len(got) == 1 and proc.reaped,
          "queue EOF -> 'exit' + reaped")

    got, reason, proc = _turn([(0.02, "obj", (DONE, 0))])
    check(reason == "exit" and got == [] and proc.reaped, "DONE sentinel -> 'exit'")
    got, reason, proc = _turn([(0.02, "obj", (EXC, "tb"))])
    check(reason == "exit" and got == [] and proc.reaped, "EXC sentinel -> 'exit'")


def test_kind_filter() -> None:
    print("[offline] ask_turn: only `kinds` objects count as turns")
    got, reason, _ = _turn([(0.02, "obj", {"kind": "noise"}),
                            (0.04, "obj", {"kind": "t", "n": 1})])
    check([o.get("kind") for o in got] == ["t"], "noise kind dropped")
    got, reason, _ = _turn([(0.02, "obj", {"kind": "noise"})])
    check(reason == "idle" and got == [], "noise alone never settles as 'turn'")


def test_pty_quiet() -> None:
    print("[offline] ask_turn: pty_quiet holds the settle while the TUI paints")
    # turn at 20ms, then paints every 50ms until 0.6s; idle=0.15 would settle
    # at ~0.17s without the gate. pty_quiet=0.3 must hold until painting stops.
    paints: list[ScriptAction] = [(0.02, "obj", {"kind": "t"})]
    paints.extend((0.05 * i, "paint") for i in range(1, 13))
    t0 = time.time()
    got, reason, _ = _turn(paints, pty_quiet=0.3, timeout=5.0)
    took = time.time() - t0
    check(reason == "turn" and len(got) == 1, "still settles as 'turn'")
    check(took >= 0.8, f"settle deferred past the painting ({took:.2f}s)")
    t0 = time.time()
    got, reason, _ = _turn([(0.02, "obj", {"kind": "t"})], pty_quiet=0.0)
    check(time.time() - t0 < 0.8, "pty_quiet=0.0 keeps the historical break")


def test_pending() -> None:
    print("[offline] ask_turn: pending() suppresses the idle break, timeout still ends it")
    # the decoded turn owes tool results forever -> only the deadline can end it
    t0 = time.time()
    got, reason, _ = _turn([(0.02, "obj", {"kind": "t", "tools": True})],
                           pending=lambda objs: bool(objs and objs[-1].get("tools")),
                           timeout=0.6)
    check(reason == "deadline" and time.time() - t0 >= 0.55,
          "pending=True turn rides to the deadline")
    # a second decoded turn clears the debt -> settles as a normal turn
    got, reason, _ = _turn([(0.02, "obj", {"kind": "t", "tools": True}),
                            (0.10, "obj", {"kind": "t", "tools": False})],
                           pending=lambda objs: bool(objs and objs[-1].get("tools")),
                           timeout=5.0)
    check(reason == "turn" and len(got) == 2, "cleared pending settles as 'turn'")


class FakeSessionProc(FakeProc):
    """FakeProc plus the constructor/lifecycle surface WireSession drives."""
    made: list[FakeSessionProc] = []

    def __init__(self, q: ResultChannel, script: Sequence[ScriptAction] = (),
                 args: tuple[ResultChannel, tuple[str, ...], float | None] | None = None) -> None:
        super().__init__(q, script)
        self._args: tuple[object, ...] = args or ()
        self.transcript = ""
        self.closed = 0
        self.start_error: BaseException | None = None
        FakeSessionProc.made.append(self)

    def start(self) -> None:
        # the real writer-close crash detection needs a live writer; the fake
        # queue was created by the base, which then closes its writer copy.
        if self.start_error is not None:
            raise self.start_error

    def reap(self) -> bool:
        if self._w.closed and self.exit_status is None:
            self.exit_status = 0
        self.reaped = self.exit_status is not None
        return self.reaped

    def close(self, *, interrupt: bool = False, grace: float = 5.0) -> None:
        self.closed += 1
        self._w.close()


class MiniSession(WireSession[TurnResult, FakeSessionProc]):
    _KINDS = ("t",)
    _IDLE = 0.15
    _PTY_IDLE = 0.3
    _READY = 0.0

    def __init__(self, script: Sequence[ScriptAction] = (), max_wait: float | None = None) -> None:
        self.timeout = 3.0
        self.idle = self._IDLE
        self._script = script
        self._max_wait = max_wait
        self.latched = 0
        self.torn_down = 0

    def _make_process(self, prompt: str | None):
        assert self._q is not None
        self._first_prompt = prompt
        return FakeSessionProc(self._q, self._script,
                               args=(self._q, self._KINDS, self._max_wait))

    def _latch_id(self) -> str:
        self.latched += 1
        return "id-1"

    def _build_response(self, objs: list[Event], reason: str) -> TurnResult:
        return (objs, reason)

    def _read_history(self) -> list[Mapping[str, object]]:
        return [{"session_id": self._id}]

    def _teardown(self) -> None:
        self.torn_down += 1


def test_wire_session_lifecycle() -> None:
    print("[offline] WireSession: lazy start, id latch, dead-process guard, close")
    s = MiniSession([(0.02, "obj", {"kind": "t", "n": 1})])
    check(s._proc is None, "no process before the first ask")
    objs, reason = s.ask("first")
    check(reason == "turn" and len(objs) == 1, "first ask returns the turn")
    assert isinstance(s._proc, FakeSessionProc)
    check(s._first_prompt == "first" and s._proc.submitted == [],
          "first prompt rode the argv (prefill), not typed")
    check(s.session_id == "id-1" and s.conversation_id == "id-1",
          "id latched once, both spellings read it")
    s.ask("second")
    check(s.latched == 1, "id latched exactly once")
    check(s._proc.submitted[-1] == "second", "follow-up typed via submit()")

    s._proc.exit_status = 1                      # simulate a died CLI
    try:
        s.ask("third")
        check(False, "dead-process guard raises")
    except RuntimeError as e:
        check("already exited" in str(e) and "id-1" in str(e),
              "dead-process guard raises with the resume id")
    proc = s._proc
    s.close(); s.close()
    check(proc.closed == 1 and s._q is None and s.torn_down == 1,
          "repeated close runs process/channel/provider cleanup exactly once")
    check(s.session_id == "id-1" and s.history() == [{"session_id": "id-1"}],
          "closed session retains its id and readable history")
    try:
        s.ask("after close")
        check(False, "closed session rejects reuse")
    except RuntimeError as error:
        check("closed" in str(error) and "id-1" in str(error),
              "closed session rejects reuse and preserves its resume id")


def test_failed_start_cleanup() -> None:
    print("[offline] WireSession: every startup failure unwinds resources immediately")
    stages = ("pre", "channel", "make", "start", "post")
    for stage in stages:
        failure = RuntimeError(f"failed {stage}")
        queues: list[ResultChannel] = []

        class FailingSession(MiniSession):
            def _pre_start(self) -> None:
                if stage == "pre":
                    raise failure

            def _make_process(self, prompt: str | None) -> FakeSessionProc:
                if stage == "make":
                    raise failure
                proc = super()._make_process(prompt)
                if stage == "start":
                    proc.start_error = failure
                return proc

            def _post_start(self) -> None:
                if stage == "post":
                    raise failure

        def channel() -> ResultChannel:
            if stage == "channel":
                raise failure
            queue = new_channel()
            queues.append(queue)
            return queue

        session = FailingSession()
        with patch("wirecap.runtime.session.new_channel", side_effect=channel):
            try:
                session.ask("go")
                check(False, f"{stage}: startup failure propagates")
            except RuntimeError as error:
                check(error is failure, f"{stage}: original startup exception survives")
        check(session._closed and session.torn_down == 1,
              f"{stage}: session is closed and provider teardown ran")
        check(all(queue._reader.closed and queue._writer.closed for queue in queues),
              f"{stage}: every allocated channel is closed")
        process = session._proc
        check(process is None or process.closed == 1, f"{stage}: allocated process was closed")
        session.close()
        check(session.torn_down == 1, f"{stage}: later close does not repeat teardown")


def test_cleanup_failure_preserves_start_error() -> None:
    print("[offline] WireSession: cleanup errors preserve the original startup error")
    failure = ValueError("cannot initialize provider")

    class BrokenCleanupSession(MiniSession):
        def _post_start(self) -> None:
            raise failure

        def _teardown(self) -> None:
            super()._teardown()
            raise RuntimeError("cleanup failed")

    session = BrokenCleanupSession()
    try:
        session.ask("go")
        check(False, "startup error propagates despite cleanup failure")
    except ValueError as error:
        check(error is failure and any("cleanup failed" in note for note in error.__notes__),
              "original startup exception contains the cleanup failure note")
    check(session._q is None and session._proc is not None and session._proc.closed == 1,
          "cleanup failure still releases process and channel")
    session.close()
    check(session.torn_down == 1, "failed teardown is not repeated on later close")


def test_process_close_failure_unwinds_session() -> None:
    print("[offline] WireSession: process-close failure still tears down provider and queue")
    session = MiniSession([(0.02, "obj", {"kind": "t"})])
    session.ask("first")
    process, queue = session._proc, session._q
    assert process is not None and queue is not None
    failure = RuntimeError("process cleanup failed")

    def fail_close(*, interrupt: bool = False, grace: float = 5.0) -> None:
        process.closed += 1
        process._w.close()
        raise failure

    with patch.object(process, "close", side_effect=fail_close):
        try:
            session.close()
            check(False, "process close error propagates")
        except RuntimeError as error:
            check(error is failure, "process close error propagates")
        session.close()
    check(queue._reader.closed and queue._writer.closed and session.torn_down == 1,
          "process error does not skip channel or provider cleanup")
    check(process.closed == 1, "process error does not cause repeated close attempts")


def test_closed_and_ended_guards() -> None:
    print("[offline] WireSession: closed and ended sessions never restart or submit")
    unopened = MiniSession()
    unopened.close()
    try:
        unopened.ask("go")
        check(False, "close before first ask forbids startup")
    except RuntimeError as error:
        check("closed" in str(error) and unopened._proc is None,
              "close before first ask forbids startup")

    session = MiniSession([(0.02, "obj", (DONE, 0))])
    _, reason = session.ask("first")
    check(reason == "exit", "terminal worker signal closes the turn")
    assert session._proc is not None
    check(session._proc.exit_status is None, "fixture keeps CLI alive after its worker ended")
    try:
        session.ask("second")
        check(False, "ended worker blocks subsequent asks while CLI still lives")
    except RuntimeError as error:
        check("already exited" in str(error) and session._proc.submitted == [],
              "ended worker blocks subsequent asks while CLI still lives")
    session.close()

    between = MiniSession([(0.02, "obj", {"kind": "t"})])
    between.ask("first")
    assert between._proc is not None
    between._proc._w.close()
    try:
        between.ask("second")
        check(False, "between-turn CLI death is reaped before submitting")
    except RuntimeError as error:
        check("already exited" in str(error) and between._proc.submitted == [],
              "between-turn CLI death is reaped before submitting")
    between.close()


def test_max_wait_assert() -> None:
    print("[offline] WireSession: max_wait=None asserted at start")
    s = MiniSession(max_wait=120)                # a numeric deadline sneaking back
    try:
        s.ask("go")
        check(False, "numeric max_wait rejected")
    except AssertionError as e:
        check("max_wait=None" in str(e), "numeric max_wait rejected")
        check(s._closed and s._q is None and s.torn_down == 1,
              "invalid child deadline unwinds its allocated resources")
    finally:
        s.close()


def test_prefill_first_off() -> None:
    print("[offline] WireSession: _PREFILL_FIRST=False types the first prompt")
    class ShellSession(MiniSession):
        _PREFILL_FIRST = False
    s = ShellSession([(0.02, "obj", {"kind": "t"})])
    s.ask("hello")
    assert isinstance(s._proc, FakeSessionProc)
    check(s._first_prompt is None and s._proc.submitted == ["hello"],
          "no argv prefill; first prompt typed like a follow-up")
    s.close()


def main() -> None:
    test_reasons()
    test_kind_filter()
    test_pty_quiet()
    test_pending()
    test_wire_session_lifecycle()
    test_failed_start_cleanup()
    test_cleanup_failure_preserves_start_error()
    test_process_close_failure_unwinds_session()
    test_closed_and_ended_guards()
    test_max_wait_assert()
    test_prefill_first_off()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
