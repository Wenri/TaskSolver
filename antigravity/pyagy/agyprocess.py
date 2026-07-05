"""AgyProcess — the single front-door for launching the agy CLI. Always instrumented
(LD_PRELOAD shim + capture JSONL, on the pinned vendor/agy). Two modes:

  * plain-CLI (``target=None``, the default) — run agy as an external process and read its
    rendered PTY transcript via ``read_until_exit`` (one-shot) / ``read_until_idle``
    (interactive). This backs ``session.run_print``, ``session.InteractiveSession``,
    ``pyagy.ask`` / ``pyagy.Session``, and the ``AgySession`` capture harness.

        p = AgyProcess(prompt="What is 2+2?"); p.start()
        print(p.read_until_exit()); p.close()

  * embedded-worker (``target=callable``) — run a pickled Python callable INSIDE agy's
    embedded interpreter and stream native objects home over a Connection (``recv``/``poll``/
    ``ask``). Use ``pyagy.agy_process.mp_child.get_result_conn()`` in the target to send home.

        from pyagy.agy_process.mp_child import get_result_conn
        def work(prompt): get_result_conn().send({"answer": 42})
        p = AgyProcess(target=work, args=("hi",)); p.start()
        print(p.recv()); p.terminate(); p.join()

Design: `AgyProcess` is a real `SpawnProcess` whose `_Popen` is `_pty.PtyPopen` — a custom
`popen_fork.Popen` that execs agy under a PTY (rather than forking python) and owns both the PTY
and the process lifecycle. In embedded-worker mode PtyPopen also hands the child two inheritable
fds (a result Pipe + a boot pipe with the pickled target) and runs `agy_process/mp_child._bootstrap`.
`start/join/exitcode/terminate` are inherited from `popen_fork.Popen` and track agy's own pid; the
result flows over the Connection (worker) or the PTY transcript (plain-CLI). This class is just the
user-facing handle that carries config to PtyPopen and exposes the two modes' APIs.
"""
import time

from multiprocessing.context import SpawnProcess

from . import conversations as _conv
from ._pty import PtyPopen


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
    """`multiprocessing.Process`-shaped handle for an agy run (see the module docstring for
    the two modes). Plain-CLI (``target=None``): drive the PTY via ``read_until_exit`` /
    ``read_until_idle`` / ``send_line`` / ``write``. Embedded-worker (``target=callable``):
    the target runs inside agy's embedded interpreter and sends objects home over a
    Connection (``recv`` / ``poll`` / ``ask``)."""

    @staticmethod
    def _Popen(process_obj):
        return PtyPopen(process_obj)

    def __init__(self, target=None, name=None, args=(), kwargs=None, *,
                 agy_bin=None, agy_args=None, prompt=None, model=None,
                 skip_permissions=False, extra_flags=None, persistent=False,
                 conversation_id=None, continue_latest=False, workdir=None,
                 capture=None, data_dir=None, trust=True, extra_env=None, echo=False,
                 daemon=None):
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

    # --- parent-side result channel (native Python objects sent by the child target) ---
    def recv(self):
        return self._popen._parent_conn.recv()

    def poll(self, timeout=0.0):
        return self._popen._parent_conn.poll(timeout)

    @property
    def connection(self):
        return self._popen._parent_conn

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

    # --- plain-CLI PTY driving (target=None): read agy's rendered transcript ---
    def read_until_exit(self, timeout=300.0):
        """Read the PTY until agy exits; return the full ANSI-stripped transcript (one-shot)."""
        return self._popen.read_until_exit(timeout=timeout)

    def read_until_idle(self, idle=6.0, timeout=180.0):
        """Read the PTY until an ``idle``-second output gap; return this slice (interactive)."""
        return self._popen.read_until_idle(idle=idle, timeout=timeout)

    @property
    def transcript(self):
        """The full ANSI-stripped transcript seen so far."""
        return self._popen.transcript

    def write(self, data):
        """Write raw bytes to the PTY."""
        self._popen.write(data)

    def send_line(self, text):
        """Type a line + Enter into the PTY."""
        self._popen.send_line(text)

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
        """agy's raw waitpid exit status, or None if still running."""
        return self._popen.status

    def close(self, interrupt=False):
        """Stop agy (``interrupt=True`` presses Ctrl-C first, for the TUI) and close the PTY."""
        self._popen.close(interrupt=interrupt)

    # --- persistent (multi-turn TUI) driving: type prompts into agy, collect decoded turns ---
    def send(self, prompt):
        """Type + submit a prompt into agy's interactive TUI (fire-and-forget)."""
        self._popen.send_line(prompt)
        self._popen._last_output = time.time()

    def ask(self, prompt=None, idle=6.0, pty_idle=15.0, timeout=180.0, ready=2.5):
        """Persistent mode: submit a prompt and return the decoded genai_turn(s) for it.
        prompt=None submits the `--prompt-interactive` prefill (use for the first turn).
        Waits for agy to be idle/ready before typing, settles when no new turn arrives for
        `idle`s (or agy stays quiet `pty_idle`s with no turn), or after `timeout`. Reads
        decoded turns off the Connection that the in-agy stream_turns target streams home."""
        pop = self._popen
        rstart = time.time()                 # wait until agy is ready (TUI drawn / prior turn done)
        while time.time() - rstart < 30 and time.time() - pop._last_output < ready:
            time.sleep(0.2)
        if prompt is None:
            pop.write(b"\r")                 # submit the prefilled initial prompt
        else:
            pop.send_line(prompt)            # type + submit a follow-up
        pop._last_output = time.time()
        conn = pop._parent_conn
        turns, last_turn, start = [], None, time.time()
        while time.time() - start < timeout:
            while conn.poll(0):
                try:
                    o = conn.recv()
                except EOFError:
                    return turns
                if isinstance(o, dict) and o.get("kind") == "genai_turn":
                    turns.append(o)
                    last_turn = time.time()
            now = time.time()
            if last_turn is not None and now - last_turn >= idle:
                break                        # turn(s) settled
            if last_turn is None and now - pop._last_output >= pty_idle:
                break                        # agy went idle without producing a turn
            time.sleep(0.2)
        return turns
