"""AgyProcess — the single front-door for launching the agy CLI. Always instrumented (LD_PRELOAD
shim + capture JSONL, on the pinned vendor/agy), and always runs an in-agy worker target that
streams agy's DECODED answer home over a SimpleQueue.

A ``target`` (default: ``agy_process.mp_child.stream_turns``) runs inside agy's embedded interpreter
and puts decoded objects home; the parent collects them with ``collect()`` (one-shot) / ``ask()``
(persistent multi-turn), or reads raw objects with ``recv``/``poll``. The PTY transcript is drained
as a byproduct and kept on ``transcript`` for diagnostics (crashes, and a fallback answer when no
turn decodes).

    # one-shot: collect agy's decoded answer turns
    p = AgyProcess(prompt="What is 2+2?"); p.start()
    turns = p.collect(); p.close()

    # custom worker: run a Python callable inside agy and stream your own objects home
    from pyagy.agy_process.mp_child import get_result_conn
    def work(x): get_result_conn().put({"x": x})
    p = AgyProcess(target=work, args=(41,)); p.start()
    print(p.recv()); p.terminate(); p.join()

Design: `AgyProcess` is a `SpawnProcess` whose `_Popen` is `_pty.PtyPopen` — a custom
`popen_fork.Popen` that execs agy under a PTY (not python), owns the PTY + the lifecycle, hands the
child a result SimpleQueue + a boot pipe with the pickled target, and runs `mp_child._bootstrap`.
`start/join/exitcode/terminate` are inherited and track agy's pid; the answer flows over the
SimpleQueue, the transcript over the PTY (a byproduct on ``transcript``).
"""
import time

import multiprocessing.connection as _conn
from multiprocessing.context import SpawnProcess

from . import conversations as _conv
from ._pty import PtyPopen

_ANSWER_KINDS = ("genai_turn", "app_response")   # decoded objects the default target streams home


def _agy_argv(prompt, persistent, model, skip_permissions, extra_flags,
              conversation_id, continue_latest):
    """agy's argv tail (everything after the binary). One-shot uses ``--print``, persistent
    uses ``--prompt-interactive``; ``conversation_id`` / ``continue_latest`` resume a stored
    conversation (both compose with either flag)."""
    flag = "--prompt-interactive" if persistent else "--print"
    argv = [flag, prompt if prompt is not None else "agy-mp"]
    if model:
        argv += ["--model", model]
    if conversation_id:
        argv.append(f"--conversation={conversation_id}")
    elif continue_latest:
        argv.append("--continue")
    if skip_permissions:
        argv.append("--dangerously-skip-permissions")
    if extra_flags:
        argv += list(extra_flags)
    return argv


class AgyProcess(SpawnProcess):
    """`multiprocessing.Process`-shaped handle for an agy run (always instrumented, always a
    worker). Collect the decoded answer with ``collect()`` (one-shot) / ``ask()`` (persistent), or
    read raw objects a custom ``target`` sends with ``recv``/``poll``. The PTY transcript is a
    byproduct on ``transcript`` (used for crash/fallback diagnostics)."""

    @staticmethod
    def _Popen(process_obj):
        return PtyPopen(process_obj)

    def __init__(self, target=None, name=None, args=(), kwargs=None, *,
                 agy_bin=None, agy_args=None, prompt=None, model=None,
                 skip_permissions=False, extra_flags=None, persistent=False,
                 conversation_id=None, continue_latest=False, workdir=None,
                 capture=None, data_dir=None, trust=True, extra_env=None, echo=False,
                 daemon=None):
        if target is None:               # default worker: stream agy's decoded answer home
            from .agy_process.mp_child import stream_turns
            target = stream_turns
        super().__init__(group=None, target=target, name=name,
                         args=args, kwargs=(kwargs or {}), daemon=daemon)
        # argv: an explicit agy_args tail wins; else assemble it from the flags. One-shot uses
        # `agy --print <prompt>`; persistent uses `--prompt-interactive` (drive via .ask()/.send()).
        self._agy_args = agy_args if agy_args is not None else _agy_argv(
            prompt, persistent, model, skip_permissions, extra_flags,
            conversation_id, continue_latest)
        self._agy_bin = agy_bin        # agy binary (default: the pinned vendor/agy)
        self._workdir = workdir        # git workspace (default: a throwaway repo)
        self._capture = capture
        self._persistent = persistent  # long-lived interactive agy (drive via .ask()/.send())
        self._conversation_id = conversation_id  # resume id; else captured after first turn
        self._data_dir = data_dir      # scope the conversation store to a project repo
        self._trust = trust            # pre-trust the workspace (no folder-trust prompt)
        self._extra_env = extra_env    # caller overlays layered onto instrumented_env (shim knobs)
        self._echo = echo              # mirror agy's PTY output to our stdout (debug)

    # --- parent-side result channel (native Python objects the target puts on the queue) ---
    def recv(self):
        return self._popen._result_q.get()

    def poll(self, timeout=0.0):
        """True if a result is waiting on the queue. Drains agy's PTY (+ auto-answers) while it
        waits, so a poll()/recv() loop keeps agy unblocked (no background pump thread)."""
        return self._popen._service(timeout)

    @property
    def connection(self):
        """The result `SimpleQueue` (native mp channel). Custom targets ``.put`` on it inside agy;
        the parent ``.get()``s it (or uses ``recv``/``poll``/``collect``)."""
        return self._popen._result_q

    @property
    def conversation_id(self):
        """agy's native conversation id for this run — the resume id, or the one captured
        after launch (from the shim's conversation_id event, else the newest store db).
        Persist it and pass ``conversation_id=`` to a new AgyProcess (or ``pyagy.resume``)
        to continue this conversation with full prior context."""
        if self._conversation_id is None and getattr(self, "_popen", None) is not None:
            self._conversation_id = _conv.capture_conversation_id(
                getattr(self._popen, "_snap", None),
                capture_path=getattr(self._popen, "_capture_path", None),
                home=getattr(self._popen, "_home", None))
        return self._conversation_id

    # --- collect the decoded answer the worker target streams home ---
    # NB: named `collect`, not `run` — `run` is BaseProcess's target-runner (called by _bootstrap
    # inside agy), which we must NOT override.
    def collect(self, timeout=300.0, kinds=_ANSWER_KINDS):
        """One-shot: run agy to completion — drain the PTY into ``transcript`` and collect the
        decoded objects (of ``kinds``) the target streams home — until agy exits / the target
        signals done / ``timeout``. Returns the collected dicts in arrival order (possibly empty
        if the run produced no decodable turn; use ``transcript`` as the fallback)."""
        pop = self._popen
        got, start = [], time.time()
        while time.time() - start < timeout:
            if pop._service(1.0):            # drains the PTY; True when the queue is readable
                try:
                    while pop._result_reader.poll(0):
                        o = pop._result_q.get()
                        if isinstance(o, tuple) and o and o[0] in ("_agy_done", "_agy_exc"):
                            pop.exited()      # reap agy so exit_status is set
                            return got
                        if isinstance(o, dict) and o.get("kind") in kinds:
                            got.append(o)
                except EOFError:
                    pop.exited()             # agy exited — the normal one-shot completion signal
                    return got
        return got

    def ask(self, prompt=None, idle=6.0, pty_idle=15.0, timeout=180.0, ready=2.5,
            kinds=_ANSWER_KINDS):
        """Persistent multi-turn: submit ``prompt`` (or the ``--prompt-interactive`` prefill if
        None), then collect the decoded objects (of ``kinds``) for that turn until it settles (no
        new object for ``idle`` s, or agy stays quiet ``pty_idle`` s with none), or ``timeout``.
        Drains the PTY meanwhile. Returns the collected dicts."""
        pop = self._popen
        rstart = time.time()                 # wait until agy is ready (TUI drawn / prior turn done)
        while time.time() - rstart < 30 and time.time() - pop._last_output < ready:
            pop._service(0.2)                # drain the PTY while waiting for agy to settle
        if prompt is None:
            pop.write(b"\r")                 # submit the prefilled initial prompt
        else:
            pop.send_line(prompt)            # type + submit a follow-up
        pop._last_output = time.time()
        got, last, start = [], None, time.time()
        while time.time() - start < timeout:
            if pop._service(0.2):            # drains the PTY; True once a result is ready
                while pop._result_reader.poll(0):
                    try:
                        o = pop._result_q.get()
                    except EOFError:
                        return got
                    if isinstance(o, dict) and o.get("kind") in kinds:
                        got.append(o)
                        last = time.time()
            now = time.time()
            if last is not None and now - last >= idle:
                break                        # turn(s) settled
            if last is None and now - pop._last_output >= pty_idle:
                break                        # agy went idle without producing a turn
        return got

    # --- PTY: raw input + the transcript byproduct ---
    @property
    def transcript(self):
        """The full ANSI-stripped PTY transcript seen so far (diagnostics / fallback answer)."""
        return self._popen.transcript

    def write(self, data):
        """Write raw bytes to the PTY."""
        self._popen.write(data)

    def send_line(self, text):
        """Type a line + Enter into the PTY."""
        self._popen.send_line(text)

    def send(self, prompt):
        """Type + submit a prompt into agy's interactive TUI (fire-and-forget)."""
        self._popen.send_line(prompt)
        self._popen._last_output = time.time()

    @property
    def workspace(self):
        """The resolved git workspace agy ran in."""
        return getattr(self._popen, "_workspace", None)

    @property
    def home(self):
        """The scoped HOME for this run (data_dir scoping), or None for the global store."""
        return getattr(self._popen, "_home", None)

    @property
    def exit_status(self):
        """agy's raw waitpid exit status, or None if not yet reaped."""
        return self._popen.status

    def close(self, interrupt=False):
        """Stop agy (``interrupt=True`` presses Ctrl-C first, for the TUI) and close the PTY."""
        self._popen.close(interrupt=interrupt)
        # We reap agy ourselves (agy owns its lifetime), so multiprocessing's active-children set
        # never sees it exit — drop it, else this Process (and its result SimpleQueue's two named
        # semaphores) is pinned in `_children` and leaks until interpreter exit instead of being
        # GC'd + sem_unlinked once the caller releases the handle.
        from multiprocessing.process import _children
        _children.discard(self)


def collect_many(procs, timeout=300.0, kinds=_ANSWER_KINDS):
    """One-shot collect from several already-``start()``ed AgyProcesses concurrently, in one
    event loop — the native way to exploit that ``start()`` is non-blocking (it forks agy and
    returns) and the result Connection + PTY are selectable fds. No threads: the forks happened
    serially in the caller, so there is no fork-in-a-multithreaded-process hazard.

    A single ``_conn.wait`` watches every live process's Connection + PTY master together, so all
    PTYs are drained (none stalls on a full buffer) while each process's ``kinds`` objects are
    gathered until it signals done (``_agy_done``/``_agy_exc``) or its Connection EOFs. Returns a
    list parallel to ``procs`` — each entry is that process's collected dicts in arrival order
    (possibly empty; use its ``.transcript`` as the fallback). Stops early once all are done, or
    at ``timeout``."""
    pops = [p._popen for p in procs]
    got = [[] for _ in procs]
    done = [False] * len(procs)
    end = time.time() + timeout
    while not all(done) and time.time() < end:
        watch = {}                              # Connection/fd -> (index, is_conn)
        for i, pop in enumerate(pops):
            if done[i]:
                continue
            watch[pop._result_reader] = (i, True)
            if not pop._pty_dead:
                watch[pop.fd] = (i, False)      # raw PTY master; drop it once it EOFs
        if not watch:
            break
        for r in _conn.wait(list(watch), max(0.0, end - time.time())):
            i, is_conn = watch[r]
            pop = pops[i]
            if not is_conn:                     # PTY readable: drain it (+ auto-answer prompts)
                if not pop._read_available():
                    pop._pty_dead = True
                continue
            try:                                # queue readable: gather this proc's results
                while pop._result_reader.poll(0):
                    o = pop._result_q.get()
                    if isinstance(o, tuple) and o and o[0] in ("_agy_done", "_agy_exc"):
                        pop.exited()             # reap agy so exit_status is set
                        done[i] = True
                        break
                    if isinstance(o, dict) and o.get("kind") in kinds:
                        got[i].append(o)
            except EOFError:
                pop.exited()                     # agy exited — the normal completion signal
                done[i] = True
    return got
