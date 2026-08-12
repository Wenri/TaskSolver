"""The shared persistent-session turn loop for the instrumented CLIs.

pyagy and pycodex each grew a structurally identical `_ask_turn` (submit a
prompt into a live TUI, drain decoded turns off the mp channel until the turn
settles) and identical channel plumbing around a spawn-context SimpleQueue.
This module is the single copy both delegate to; pykimi's Session builds on it
directly.

Parent-side runtime code: this imports only stdlib + `wirecap.decode.mp_child`
sentinels (decode never imports back — the `python3 -S` purity probes guard
that direction, not this one).
"""

from __future__ import annotations

import multiprocessing as _mp
import time

from ..decode.mp_child import DONE as _DONE, EXC as _EXC

_SPAWN = _mp.get_context("spawn")

__all__ = ["new_channel", "close_channel", "ask_turn", "WireSession"]


def new_channel():
    """A spawn-context result SimpleQueue for one CLI run. The caller keeps the
    reader; the child (the CLI) inherits both ends across execve and drops the
    reader. Close the parent's writer right after ``proc.start()`` so the
    reader EOFs when the CLI dies (crash detection)."""
    return _SPAWN.SimpleQueue()


def close_channel(q):
    """Tear down the queue's pipe ends (idempotent). Its named SemLocks unlink
    via their own resource_tracker Finalize when ``q`` is GC'd; this just makes
    the fd close prompt."""
    for c in (q._reader, q._writer):
        try:
            c.close()
        except Exception:
            pass


def ask_turn(proc, q, prompt=None, *, kinds, idle, pty_idle, timeout,
             ready=2.5, pty_quiet=0.0, pending=None):
    """One turn against a live interactive CLI: submit ``prompt``, then drain
    decoded objects (of ``kinds``) off ``q`` until the turn settles. Returns
    ``(objs, reason)``.

    The hoist of ``pyagy._ask_turn`` / ``pycodex._ask_turn`` — behaviour
    identical at the historical defaults (``pty_quiet=0.0``, ``pending=None``),
    with four additions the graded Session port needs:

    * ``kinds`` is required-keyword: each provider names its own decoded-turn
      kind(s); there is no sensible cross-provider default.
    * the settle ``reason`` is returned instead of discarded — the caller's
      return-code mapping depends on *why* the turn ended:
        - ``"turn"``      a decoded turn arrived and the stream then stayed
                          quiet for ``idle`` s — the normal boundary;
        - ``"idle"``      no decoded turn, and the CLI's PTY stayed quiet for
                          ``pty_idle`` s (auth prompt, spend cap, tool-only
                          reply — something that never decodes);
        - ``"deadline"``  ``timeout`` elapsed mid-turn — the caller should
                          treat the turn as timed out, NOT as a clean answer;
        - ``"exit"``      the CLI died or the in-CLI target signalled
                          done/raised — nothing more will ever arrive.
    * ``pty_quiet`` (seconds): suppress the ``idle`` settle while the PTY is
      still painting. An agentic turn is many API responses — a decoded turn
      followed by ``idle`` s of *stream* silence can still be mid-tool-call,
      and both TUIs repaint while working, so "stream quiet AND terminal
      quiet" is a far safer boundary. ``0.0`` preserves the historical break.
    * ``pending`` (callable ``objs -> bool``): a deterministic "the last
      decoded turn requested tool calls, so it cannot be the end" hook; while
      it returns True the ``idle`` break is suppressed entirely (``timeout``
      still applies — a hung tool must surface as ``"deadline"``, not ride
      forever).

    ``ready`` waits for the TUI to settle (screen drawn / prior turn done)
    before typing, so the prompt is not swallowed. ``prompt=None`` submits the
    prefill already on the CLI's argv (a bare CR).
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

    got, last, start = [], None, time.time()
    reason = "deadline"
    while time.time() - start < timeout:
        if proc.service_pty(0.2, [reader]):
            while reader.poll(0):
                try:
                    o = q.get()
                except EOFError:
                    proc.reap()          # the CLI exited — nothing more will ever arrive
                    return got, "exit"
                if isinstance(o, tuple) and o and o[0] in (_DONE, _EXC):
                    proc.reap()          # the in-CLI target finished/raised: the stream is
                    return got, "exit"   # over — end the turn, don't wait out pty_idle
                if isinstance(o, dict) and o.get("kind") in kinds:
                    got.append(o)
                    last = time.time()
        now = time.time()
        if last is not None and now - last >= idle:
            if pending is not None and pending(got):
                continue                 # decoded turn still owes tool results — not the end
            if now - proc.last_output < pty_quiet:
                continue                 # the CLI is still painting — not the end either
            reason = "turn"              # turn(s) settled
            break
        if last is None and now - proc.last_output >= pty_idle:
            reason = "idle"              # the CLI went idle without producing a turn
            break
    return got, reason


class WireSession:
    """Base for a multi-turn session over one live instrumented CLI.

    A provider subclass supplies its process construction and response shape;
    the base owns the lifecycle every provider had duplicated: lazy start on
    the first ``ask``, the dead-process guard, the channel plumbing, the turn
    loop (via :func:`ask_turn`), id latching, and idempotent ``close``.

    Class attrs (each provider's turn-boundary personality):
      ``_KINDS``          decoded-object kinds that count as this CLI's turns
      ``_IDLE``           stream-settle seconds (no new decoded turn)
      ``_PTY_IDLE``       PTY-idle seconds when no turn ever decodes
      ``_READY``          TUI settle wait before typing (default 2.5)
      ``_PTY_QUIET``      terminal-quiet gate on the idle break (default 0.0)
      ``_PREFILL_FIRST``  True: the first prompt rides the CLI argv and turn 1
                          submits the prefill (bare CR); False: the CLI takes
                          no positional prompt and turn 1 types it like any
                          follow-up (kimi shell mode)
      ``_ID_KW``          the provider's native id spelling, for messages

    Hooks (required): ``_make_process(prompt)`` builds the un-started process
    (its mp args MUST carry ``max_wait=None`` — asserted below);
    ``_latch_id()`` returns the native session/conversation id once known;
    ``_build_response(objs, reason)`` shapes the provider Response.
    Hooks (optional): ``_pre_start``, ``_post_start``, ``_teardown``,
    ``_pending(objs)`` (tool-calls-outstanding — wired into ``ask_turn``), and
    ``_load_capture_tail()`` when ``capture_tail=True`` is supported.

    Naming: ``session_id`` is the cross-provider name and
    ``conversation_id`` its permanent alias — constructor kwargs keep each
    provider's own spelling (renaming those would break ``pyagy.resume``,
    ``AgyModel.session()`` and every existing caller).

    Capture authority: a Session's ``ask`` is **stream-authoritative** on
    purpose. The one-shot ``ask()`` helpers prefer the capture JSONL because
    an *exiting* CLI can drop the last streamed turn — a failure mode that
    structurally cannot happen mid-session (the CLI is still alive when the
    turn settles). And a Session's capture is *cumulative*: treating it as
    authoritative would make turn N's response contain turns 1..N. Callers
    that still want the capture's view opt in with ``capture_tail=True``,
    which reads only the records appended since the previous turn (the
    provider keeps the cursor) and falls back to the streamed objects when
    the tail is empty.
    """

    _KINDS: tuple = ()
    _IDLE = 8.0
    _PTY_IDLE = 25.0
    _READY = 2.5
    _PTY_QUIET = 0.0
    _PREFILL_FIRST = True
    _ID_KW = "session_id"

    # shared instance state (providers set the rest in their __init__)
    _proc = None
    _q = None
    _id = None
    capture_tail = False

    # -- hooks ----------------------------------------------------------------
    def _make_process(self, prompt):
        raise NotImplementedError

    def _latch_id(self):
        raise NotImplementedError

    def _build_response(self, objs, reason):
        raise NotImplementedError

    def _read_history(self):
        return []

    def _pre_start(self):
        pass

    def _post_start(self):
        pass

    def _teardown(self):
        pass

    def _pending(self, objs):
        return False

    def _load_capture_tail(self):
        raise NotImplementedError(
            f"{type(self).__name__} does not support capture_tail")

    # -- lifecycle -------------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _start(self, prompt):
        self._pre_start()
        self._q = new_channel()
        self._proc = self._make_process(prompt)
        # A Session's life IS the CLI's life: caller think-time between turns
        # is unbounded, so the in-CLI target must have NO deadline — its exit
        # is death-based (close()/GC kill the CLI, which ends the target; see
        # mp_child's max_wait contract). A numeric deadline here would kill
        # the bridge mid-session after that many seconds of *wall time*, not
        # inactivity — the bug this assert keeps from coming back.
        args = getattr(self._proc, "_args", None)
        assert args and len(args) >= 3 and args[2] is None, \
            "WireSession processes must be built with max_wait=None"
        self._proc.start()
        self._q._writer.close()   # parent reads only; reader EOFs on CLI death
        self._post_start()

    def ask(self, prompt):
        """Send ``prompt`` (starting the session on the first call) and return
        the provider Response for the turn it produced."""
        if self._proc is not None and self._proc.exit_status is not None:
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
        if self.capture_tail:
            objs = self._load_capture_tail() or objs
        if self._id is None:
            self._id = self._latch_id()
        return self._build_response(objs, reason)

    def _turn(self, prompt):
        return ask_turn(self._proc, self._q, prompt, kinds=self._KINDS,
                        idle=self.idle, pty_idle=self._PTY_IDLE,
                        timeout=self.timeout, ready=self._READY,
                        pty_quiet=self._PTY_QUIET, pending=self._pending)

    @property
    def session_id(self):
        """The CLI's native session/conversation id — the resumed id, or the
        one latched after the first turn. Persist it to resume later."""
        return self._id

    #: Cross-provider alias (agy spells it ``conversation_id``).
    conversation_id = session_id

    def history(self):
        """The stored transcript, read from the CLI's own store; empty until
        an id is known."""
        if not self._id:
            return []
        return self._read_history()

    def close(self):
        try:
            if self._proc is not None:
                self._proc.close(interrupt=True)
        finally:
            if self._q is not None:
                close_channel(self._q)
            self._teardown()
