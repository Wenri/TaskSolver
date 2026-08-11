"""KimiProcess / KimiPopen — kimi-code driven as a wirecap mp-child, streaming decoded
``kimi_turn``s home over a caller-owned ``SimpleQueue``.

The kimi sibling of pycodex's CodexProcess/CodexPopen, on the shared ``wirecap.runtime.pty``
bases — so ``kimi -p``, ``codex exec`` and ``agy --print`` run on identical machinery. Two
codex-matching deviations from the PTY defaults, for the same reasons:

  * print mode points the child's stdin at ``/dev/null`` — there is nothing to type into it
    (the prompt rides ``-p`` on the argv), and a PTY-slave stdin would leave the CLI thinking
    a user is attached;
  * the death sentinel is ``os.pidfd_open`` rather than the PTY master — kimi-code spawns MCP
    stdio servers and node-pty shell grandchildren that inherit the slave, so the master can
    outlive the CLI itself.

The bridge (loaded by the vendored wiretap patch via ``$WIRE_NODE_ADDON``) runs
``wirecap.decode.mp_child``, whose ``stream_turns`` target ``.put``s ``kimi_turn``s over the
queue; the durable ``WIRE_CAPTURE`` JSONL stays authoritative for the returned turns (client.py).
"""
import os

from wirecap.runtime.pty import WirePtyPopen, WirePtyProcess

from ._env import instrumented_env, kimi_argv


class KimiPopen(WirePtyPopen):
    """``WirePtyPopen`` for a kimi-code run. Reads its config off ``process_obj``
    (``_prompt``/``_workdir``/``_capture``/``_model``/``_extra_flags``/``_kimi_bin``/``_extra_env``/
    ``_kimi_home``/``_session_id``/``_continue_latest``); the PTY mechanics come from
    ``WirePtyPopen`` and the boot channel + lifecycle from ``WirePopen``. Print mode never shows
    an interactive prompt (approvals come from the run's config/permission rules), so there is no
    ``_answer`` hook."""
    method = "kimi"
    _stdin_devnull = True    # print mode: nothing is typed; -p carries the prompt

    def _resolve_launch(self, process_obj):
        workdir = process_obj._workdir
        self._workspace = workdir
        capture = process_obj._capture
        self._capture_path = capture
        # instrumented_env sets WIRE_ENABLE/WIRE_MODULE/WIRE_CAPTURE/WIRE_NODE_ADDON/PYTHONHOME/
        # KIMI_CODE_HOME/KIMI_MODEL_NAME (+ extra_env); the base (WirePopen._launch) adds
        # WIRE_MP_BOOT_FD.
        env = instrumented_env(capture, extra_env=process_obj._extra_env,
                               kimi_home=getattr(process_obj, "_kimi_home", None),
                               model=process_obj._model)
        argv = kimi_argv(process_obj._prompt, extra_flags=process_obj._extra_flags,
                         kimi_bin=process_obj._kimi_bin,
                         session_id=getattr(process_obj, "_session_id", None),
                         continue_latest=getattr(process_obj, "_continue_latest", False))
        return argv, env, workdir

    def _make_sentinel(self):
        """kimi-code's MCP/shell grandchildren inherit the pty slave, so the master can stay open
        after the CLI itself exits — track the process instead. Falls back to no sentinel (the
        drain polls ``reap()``) on a kernel without pidfd."""
        try:
            return os.pidfd_open(self.pid)
        except (AttributeError, OSError):
            return None


class KimiProcess(WirePtyProcess):
    """``WirePtyProcess`` handle for a kimi-code run — the kimi twin of ``CodexProcess``, on the
    same PTY machinery. The caller creates the result ``SimpleQueue`` and passes it via
    ``args=(q, ("kimi_turn",), max_wait)``; the default target
    (``wirecap.decode.mp_child.stream_turns``) streams the decoded turns home, and the caller
    drains with ``service_pty(timeout, [q._reader])`` + ``q.get()``.

    One-shot print mode only: kimi-code's interactive shell UI is not driven here (the
    cross-run resume story rides the native store via ``session_id``/``continue_latest``
    instead — see ``pykimi.ask``)."""

    @staticmethod
    def _Popen(process_obj):
        return KimiPopen(process_obj)

    def __init__(self, prompt=None, target=None, name=None, args=(), kwargs=None, *,
                 workdir=None, capture=None, model=None, extra_flags=None,
                 kimi_bin=None, extra_env=None, session_id=None,
                 continue_latest=False, kimi_home=None, echo=False, daemon=None):
        super().__init__(target=target, name=name, args=args, kwargs=kwargs, daemon=daemon)
        self._prompt = prompt
        self._workdir = workdir
        self._capture = capture
        self._model = model                    # -> KIMI_MODEL_NAME (env-family model definition)
        self._extra_flags = extra_flags
        self._kimi_bin = kimi_bin              # test stub substitute for `node main.mjs`
        self._extra_env = extra_env
        self._session_id = session_id          # resume a stored session (-S <id>)
        self._continue_latest = continue_latest  # resume the newest for the workdir (--continue)
        self._kimi_home = kimi_home            # first-class KIMI_CODE_HOME scoping (config/sessions)
        self._echo = echo                      # mirror the CLI's PTY output to our stdout (debug)
