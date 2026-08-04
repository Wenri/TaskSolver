"""PtyPopen — launch agy under a PTY as a multiprocessing spawn child, and BE the PTY handler.

`agy` inspects its controlling terminal and refuses to behave without a real TTY, so we fork it
under a pty; it also blocks on terminal-capability queries + the folder-trust menu until answered.

Everything generic about running a CLI under a pty — the fork, the winsize, reading/echoing the
master, the transcript byproduct, `_service`/`service_many`, and the PTY-master death sentinel —
lives in the shared `wirecap.runtime.pty.WirePtyPopen`; the generic spawn-child machinery (boot
channel, fd inheritance, lifecycle) lives under that in `wirecap.runtime.process.WirePopen`. This
subclass is the agy-specific remainder: `_resolve_launch` (agy's instrumented argv/env via
PT_INTERP+--preload, the repo-scoped conversation store), `_answer` (agy's terminal-query +
folder-trust auto-reply), and `_interrupt` (Ctrl-C the TUI on close).

Every launch is instrumented (the shim is injected via agy's PT_INTERP + --preload — never
LD_PRELOAD, which leaks into agy's children — plus capture on the pinned vendor/agy). Like
`popen_spawn_posix`, this Popen owns the fork + fd inheritance but NOT the result queue: the caller
(client.py) creates the SimpleQueue, passes it as a target arg, and drains it via `_service`.
See `wirecap/decode/mp_child.py` (child side) + `wirecap/runtime/{process,pty}.py`.

`AgyProcess` (pyagy/agyprocess.py) is the user-facing `SpawnProcess` handle; this is its `_Popen`.
"""
import os
import time

from wirecap.runtime.pty import WirePtyPopen, service_many  # noqa: F401  (service_many re-exported
#                                                              for client.py's _collect_many)

from . import conversations as _conv
from ._env import _vendored, instrumented_env, preload_argv
from ._term import answer_queries, answer_trust
from .conversations import ensure_git_workspace

# the pinned agy whose build-id matches the shim: bundled pyagy/vendor/agy (wheel) or the
# sibling antigravity/vendor/agy (checkout) — never an external path.
_VENDOR_AGY = _vendored("vendor/agy", "../vendor/agy")


class PtyPopen(WirePtyPopen):
    """The `WirePtyPopen` for an agy run: execs agy under a PTY with agy's argv/env and answers its
    startup prompts. Constructed by `AgyProcess._Popen(process_obj)`; reads its config off
    `process_obj` (`_agy_bin`, `_agy_args`, `_workdir`, `_capture`, `_data_dir`, `_trust`,
    `_extra_env`, `_echo`). The PTY mechanics and the generic boot-channel/lifecycle are inherited."""
    method = "agy"

    # --- launch hooks (fill the WirePtyPopen/WirePopen base) -----------------
    def _resolve_launch(self, process_obj):
        """Build agy's instrumented argv + env (the base adds WIRE_MP_BOOT_FD). Records the run's
        resolved workspace/capture/home for AgyProcess's accessors."""
        # instrumentation needs the build-id-matched binary: an explicit programmatic agy_bin
        # (tests inject one), else the packaged agy (_VENDOR_AGY — bundled or sibling, never external).
        agy = getattr(process_obj, "_agy_bin", None) or _VENDOR_AGY
        workdir = ensure_git_workspace(getattr(process_obj, "_workdir", None))
        self._workspace = workdir                            # resolved workspace (AgyProcess.workspace)
        capture = getattr(process_obj, "_capture", None) or os.path.join(workdir, "agy-capture.jsonl")
        self._capture_path = capture        # for AgyProcess.conversation_id (conversation_id event)
        self._home, env_ovr = _conv.scope_for_run(
            workdir, getattr(process_obj, "_data_dir", None),
            trust=getattr(process_obj, "_trust", True))     # repo-scoped store + workspace trust
        # caller overlays (shim knobs / rewrite) + scoped-HOME override. The boot pipe fd is added by
        # the base (WirePopen._launch) — the worker channel it owns.
        extra = {**(getattr(process_obj, "_extra_env", None) or {}), **env_ovr}
        env = instrumented_env(capture=capture, extra_env=extra)
        agy_args = getattr(process_obj, "_agy_args", None) or ["--print", "agy-mp"]
        # Inject the shim via agy's PT_INTERP + --preload (per-exec) rather than LD_PRELOAD, which
        # every child agy spawns would inherit and needlessly load the shim into. An explicit empty
        # LD_PRELOAD in extra_env opts out (an uninstrumented baseline — e.g. test auth probes).
        if env.pop("LD_PRELOAD", None) == "":
            argv = [agy, *agy_args]
        else:
            argv = preload_argv(agy, agy_args, env=env)
        return argv, env, workdir

    def _spawn_child(self, argv, workdir, env, process_obj):
        """agy's prompt-answering state + the pre-launch conversation-store snapshot, then the
        base's pty.fork. The snapshot MUST be taken before the fork (it diffs the store afterwards
        to recover this run's conversation id)."""
        self._qpos = 0                  # terminal-query scan position
        self._trust_answered = False
        self._snap = _conv.snapshot(home=self._home)   # pre-launch store snapshot → conversation_id
        super()._spawn_child(argv, workdir, env, process_obj)

    def _answer(self):
        """Reply to agy's terminal-capability queries + the folder-trust menu (else it blocks)."""
        self._qpos = answer_queries(self.raw, self._qpos, lambda b: os.write(self.fd, b))
        if not self._trust_answered:
            self._trust_answered = answer_trust(self.raw, lambda b: os.write(self.fd, b))

    def _interrupt(self):
        """WirePopen.close hook: Ctrl-C twice to break agy out of its TUI before SIGTERM."""
        for _ in range(2):
            try:
                self.write(b"\x03")
                time.sleep(0.2)
            except OSError:
                break
