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
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)

from wirecap.runtime.session import (WireSession, ask_turn,  # noqa: E402
                                     close_channel, new_channel)
from wirecap.decode.mp_child import DONE, EXC  # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


class FakeProc:
    """Duck-type of the WirePtyProcess surface ask_turn touches. A `script` maps
    a time offset (s, from the submit) to an action: ("obj", dict) enqueues a
    decoded object, ("paint",) bumps last_output (the TUI repainted),
    ("die",) drops every writer end (EOF at the reader).

    Like the real child, the fake holds its OWN copy of the queue's writer (a
    dup'd fd) — WireSession closes the parent's writer right after start(), and
    the crash-detection EOF must still only fire when the "child" lets go."""

    def __init__(self, q, script=()):
        import multiprocessing.connection as _mpc
        self.q = q
        self._w = _mpc.Connection(os.dup(q._writer.fileno()), readable=False)
        self.script = sorted(script, key=lambda e: e[0])
        self.last_output = time.time()
        self.exit_status = None
        self.reaped = False
        self.submitted = []
        self._t0 = None

    # -- surface ---------------------------------------------------------------
    def service_pty(self, wait, readers):
        # replay any due script actions, then report reader readability
        now = time.time()
        if self._t0 is not None:
            while self.script and now - self._t0 >= self.script[0][0]:
                _, action = self.script[0][0], self.script.pop(0)[1:]
                if action[0] == "obj":
                    self._w.send(action[1])   # what SimpleQueue.put does, sans lock
                elif action[0] == "paint":
                    self.last_output = time.time()
                elif action[0] == "die":
                    self._w.close()
                    try:
                        self.q._writer.close()
                    except Exception:
                        pass
        time.sleep(min(wait, 0.02))
        return self.q._reader.poll(0)

    def write(self, data):
        self._arm()

    def submit(self, prompt):
        self.submitted.append(prompt)
        self.last_output = time.time()
        self._arm()

    def reap(self):
        self.reaped = True
        self.exit_status = 0

    def _arm(self):
        self._t0 = time.time()


def _turn(script, **kw):
    q = new_channel()
    proc = FakeProc(q, script)
    kw.setdefault("kinds", ("t",))
    kw.setdefault("idle", 0.15)
    kw.setdefault("pty_idle", 0.3)
    kw.setdefault("timeout", 3.0)
    kw.setdefault("ready", 0.0)
    got, reason = ask_turn(proc, q, "go", **kw)
    close_channel(q)
    return got, reason, proc


def test_reasons():
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


def test_kind_filter():
    print("[offline] ask_turn: only `kinds` objects count as turns")
    got, reason, _ = _turn([(0.02, "obj", {"kind": "noise"}),
                            (0.04, "obj", {"kind": "t", "n": 1})])
    check([o.get("kind") for o in got] == ["t"], "noise kind dropped")
    got, reason, _ = _turn([(0.02, "obj", {"kind": "noise"})])
    check(reason == "idle" and got == [], "noise alone never settles as 'turn'")


def test_pty_quiet():
    print("[offline] ask_turn: pty_quiet holds the settle while the TUI paints")
    # turn at 20ms, then paints every 50ms until 0.6s; idle=0.15 would settle
    # at ~0.17s without the gate. pty_quiet=0.3 must hold until painting stops.
    paints = [(0.02, "obj", {"kind": "t"})] + [(0.05 * i, "paint")
                                               for i in range(1, 13)]
    t0 = time.time()
    got, reason, _ = _turn(paints, pty_quiet=0.3, timeout=5.0)
    took = time.time() - t0
    check(reason == "turn" and len(got) == 1, "still settles as 'turn'")
    check(took >= 0.8, f"settle deferred past the painting ({took:.2f}s)")
    t0 = time.time()
    got, reason, _ = _turn([(0.02, "obj", {"kind": "t"})], pty_quiet=0.0)
    check(time.time() - t0 < 0.8, "pty_quiet=0.0 keeps the historical break")


def test_pending():
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
    made = []

    def __init__(self, q, script=(), args=None):
        super().__init__(q, script)
        self._args = args
        self.closed = 0
        FakeSessionProc.made.append(self)

    def start(self):
        # the real writer-close crash detection needs a live writer; the fake
        # queue was created by the base, which then closes its writer copy.
        pass

    def close(self, interrupt=False):
        self.closed += 1


class MiniSession(WireSession):
    _KINDS = ("t",)
    _IDLE = 0.15
    _PTY_IDLE = 0.3

    def __init__(self, script=(), max_wait=None):
        self.timeout = 3.0
        self.idle = self._IDLE
        self._script = script
        self._max_wait = max_wait
        self.latched = 0

    def _make_process(self, prompt):
        self._first_prompt = prompt
        return FakeSessionProc(self._q, self._script,
                               args=(self._q, self._KINDS, self._max_wait))

    def _latch_id(self):
        self.latched += 1
        return "id-1"

    def _build_response(self, objs, reason):
        return (objs, reason)


def test_wire_session_lifecycle():
    print("[offline] WireSession: lazy start, id latch, dead-process guard, close")
    s = MiniSession([(0.02, "obj", {"kind": "t", "n": 1})])
    check(s._proc is None, "no process before the first ask")
    objs, reason = s.ask("first")
    check(reason == "turn" and len(objs) == 1, "first ask returns the turn")
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
    check(proc.closed == 2 and s._q is not None, "close() idempotent")


def test_max_wait_assert():
    print("[offline] WireSession: max_wait=None asserted at start")
    s = MiniSession(max_wait=120)                # a numeric deadline sneaking back
    try:
        s.ask("go")
        check(False, "numeric max_wait rejected")
    except AssertionError as e:
        check("max_wait=None" in str(e), "numeric max_wait rejected")
    finally:
        s.close()


def test_prefill_first_off():
    print("[offline] WireSession: _PREFILL_FIRST=False types the first prompt")
    class ShellSession(MiniSession):
        _PREFILL_FIRST = False
    s = ShellSession([(0.02, "obj", {"kind": "t"})])
    s.ask("hello")
    check(s._first_prompt is None and s._proc.submitted == ["hello"],
          "no argv prefill; first prompt typed like a follow-up")
    s.close()


def main():
    test_reasons()
    test_kind_filter()
    test_pty_quiet()
    test_pending()
    test_wire_session_lifecycle()
    test_max_wait_assert()
    test_prefill_first_off()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
