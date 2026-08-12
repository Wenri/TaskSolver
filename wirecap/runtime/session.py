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

__all__ = ["new_channel", "close_channel", "ask_turn"]


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
