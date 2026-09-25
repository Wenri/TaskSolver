"""Official Codex Python SDK over the bundled, instrumented app-server.

The SDK handles typed threads, structured inputs and JSON-RPC. TaskSolver adds
capture, workspace/MCP configuration, a deadline and process-group cleanup.
The existing exec/PTY client remains available unchanged.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from wirecap.decode.turns import Usage
from wirecap.runtime.workspace import ensure_git_workspace

from ._env import CODEX_BIN, instrumented_env
from .client import CodexResponse, _load_capture
from .config import mcp_flags
from .sessions import latest_session_id, read_transcript

if TYPE_CHECKING:
    from openai_codex import Thread, TurnResult


@dataclass
class CodexSDKResponse(CodexResponse):
    """The compatible capture response plus the official SDK's ``TurnResult``."""

    sdk_result: TurnResult | None = None

    @property
    def explicit_reasoning_output(self):
        if self.sdk_result is None:
            return ""
        from openai_codex.generated.v2_all import ReasoningThreadItem

        fragments: Final = []
        for wrapped in self.sdk_result.items:
            item = wrapped.root
            if isinstance(item, ReasoningThreadItem):
                fragments.extend(item.summary or [])
                fragments.extend(item.content or [])
        return "\n\n".join(fragments)

    @property
    def usage(self):
        if self.turns:
            return super().usage
        if self.sdk_result is None or self.sdk_result.usage is None:
            return Usage()
        usage: Final = self.sdk_result.usage.last
        return Usage(input_tokens=usage.input_tokens,
                     cached_input_tokens=usage.cached_input_tokens,
                     output_tokens=usage.output_tokens,
                     reasoning_output_tokens=usage.reasoning_output_tokens,
                     total_tokens=usage.total_tokens,
                     raw=usage.model_dump(by_alias=True, mode="json"))


def _sdk_client(config):
    # Keep the client reachable before initialize() waits for an RPC response,
    # so a startup timeout can close it too. This small lifecycle adapter is
    # tied to the pristine SDK shipped alongside our pinned CLI source.
    from openai_codex import Codex
    from openai_codex.client import CodexClient

    client: Final = Codex.__new__(Codex)
    client._client = CodexClient(config=config)
    return client


def _stop_client(client, *, force=False):
    transport: Final = client._client
    proc: Final = transport._proc
    if proc is None:
        return None
    if force:
        # Kill before closing a buffered stdin: another thread may be blocked
        # writing a large prompt while holding that file object's lock.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            proc.kill()
    # EOF gives app-server a chance to drain native events and wire_shutdown().
    try:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        transport.close()
        # The SDK only terminates the leader. MCP/tool descendants must not
        # survive it; sdk_process establishes the group before executing Codex.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
    return proc.returncode


class SDKSession:
    """Persistent official-SDK thread with native capture and bounded ``ask``.

    ``ask`` accepts the SDK's text or typed multimodal input and all
    ``Thread.run`` options (for example ``output_schema`` and ``effort``).
    ``client`` and ``thread`` expose the official SDK for streaming, turn
    steering and other advanced operations; those direct calls use the SDK's
    own lifecycle and have no TaskSolver deadline. Use a context manager.

    New threads default to read-only sandboxing and denied escalations.
    ``thread_options`` accepts the official ``thread_start/thread_resume``
    options, including ``Sandbox`` and ``ApprovalMode`` enum values.
    A timeout closes the session; its stored thread can be resumed explicitly.
    """

    def __init__(self, *, model=None, workspace=None, timeout=300,
                 codex_bin=None, extra_env=None, mcp_servers=None,
                 codex_home=None, session_id=None, continue_latest=False,
                 capture=True, config_overrides=(), thread_options=None):
        if session_id and continue_latest:
            raise ValueError("session_id and continue_latest are mutually exclusive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        from openai_codex import ApprovalMode, CodexConfig, Sandbox

        self.workspace = ensure_git_workspace(workspace)
        self.timeout = timeout
        self.codex_home = os.path.abspath(codex_home) if codex_home else None
        self._session_id = session_id
        if continue_latest:
            self._session_id = latest_session_id(home=self.codex_home, cwd=self.workspace)
            if not self._session_id:
                raise ValueError("No saved Codex session exists in this workspace")
        self._thread_options: dict[str, Any] = dict(model=model, cwd=self.workspace,
                                                   sandbox=Sandbox.read_only,
                                                   approval_mode=ApprovalMode.deny_all)
        self._thread_options.update(thread_options or {})
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._started = False
        self._thread: Thread | None = None
        self._seen = 0
        self._exit_status = 0
        self.capture_path = ""
        if capture:
            if isinstance(capture, (str, os.PathLike)):
                self.capture_path = os.path.abspath(capture)
                os.makedirs(os.path.dirname(self.capture_path), exist_ok=True)
                with open(self.capture_path, "w"):
                    pass
            else:
                fd, self.capture_path = tempfile.mkstemp(
                    prefix="codex-sdk-", suffix=".jsonl", dir=self.workspace)
                os.close(fd)
        env: Final = instrumented_env(self.capture_path or os.devnull,
                                      extra_env=extra_env, codex_home=self.codex_home)
        # The SDK uses plain stdio, with capture rather than the mp-child stream.
        env.pop("WIRE_MP_BOOT_FD", None)
        if not capture:
            env["WIRE_ENABLE"] = "0"
        flags: Final = mcp_flags(mcp_servers) if mcp_servers else []
        for override in config_overrides:
            flags.extend(["--config", override])
        self._api_key = (env.get("OPENAI_API_KEY") or "").strip() or None
        if self._api_key:
            # app-server does not consume OPENAI_API_KEY automatically. Its
            # official login RPC must use the process-local credential store,
            # never replace a caller's existing on-disk or keyring login.
            flags.extend(["--config", 'cli_auth_credentials_store="ephemeral"'])
        binary: Final = os.path.abspath(os.fspath(codex_bin or CODEX_BIN))
        if not os.path.isfile(binary):
            raise FileNotFoundError(f"Codex binary not found: {binary}; run pixi install")
        config: Final = CodexConfig(
            codex_bin=binary, cwd=self.workspace, env=env,
            launch_args_override=(sys.executable, "-m", "pycodex.sdk_process",
                                  binary, *flags, "app-server", "--listen", "stdio://"),
            client_name="tasksolver", client_title="TaskSolver Codex SDK",
            client_version="0.157.0")
        self._client = _sdk_client(config)

    def _start(self):
        if self._closed:
            raise RuntimeError("SDKSession is closed; resume its session_id in a new session")
        if self._started:
            return
        from openai_codex._initialize_metadata import validate_initialize_metadata

        with self._close_lock:
            if self._closed:
                raise RuntimeError("SDKSession is closed")
            self._client._client.start()
        self._client._init = validate_initialize_metadata(self._client._client.initialize())
        if self._api_key:
            self._client.login_api_key(self._api_key)
        if self._session_id:
            self._thread = self._client.thread_resume(self._session_id, **self._thread_options)
        else:
            self._thread = self._client.thread_start(**self._thread_options)
        self._session_id = self._thread.id
        self._started = True

    def _bounded(self, operation):
        expired: Final = threading.Event()

        def expire():
            expired.set()
            self.close(force=True)

        timer: Final = threading.Timer(self.timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            result = operation()
        except Exception:
            if not expired.is_set():
                self.close()
                raise
            result = None
        finally:
            timer.cancel()
            timer.join()
        return result, expired.is_set()

    def _ensure_started(self):
        _, timed_out = self._bounded(self._start)
        if timed_out:
            raise TimeoutError("Codex SDK startup timed out")

    @property
    def client(self):
        """Official ``openai_codex.Codex`` client (lazy startup)."""
        self._ensure_started()
        return self._client

    @property
    def thread(self):
        """Official ``openai_codex.Thread`` (lazy startup)."""
        self._ensure_started()
        return self._thread

    @property
    def session_id(self):
        return self._session_id

    @property
    def conversation_id(self):
        return self.session_id

    def history(self):
        return read_transcript(self.session_id, home=self.codex_home) if self.session_id else []

    def ask(self, prompt, **turn_options):
        """Run one SDK turn, returning native capture plus its typed result."""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("An SDKSession supports one active ask at a time")
        try:
            def run():
                self._start()
                assert self._thread is not None
                result: Final = self._thread.run(prompt, **turn_options)
                if result.status.value != "completed":
                    raise RuntimeError(f"Codex SDK turn ended with status {result.status.value}")
                return result

            result, timed_out = self._bounded(run)
            # The bridge drains on its own thread. Allow its final event to
            # reach the line-buffered capture before taking this turn's slice.
            until: Final = time.monotonic() + 0.25
            while True:
                captured = _load_capture(self.capture_path)
                turns = captured[self._seen:]
                if (not self.capture_path or timed_out or result is None
                        or not result.final_response
                        or any(t.get("text") == result.final_response for t in turns)
                        or time.monotonic() >= until):
                    break
                time.sleep(0.01)
            self._seen = len(captured)
            return CodexSDKResponse(
                text=(result.final_response or "") if result else "", transcript="",
                turns=turns, exit_status=self._exit_status,
                capture_path=self.capture_path, workspace=self.workspace,
                timed_out=timed_out, session_id=self.session_id, sdk_result=result)
        finally:
            self._lock.release()

    def close(self, *, force=False):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._exit_status = _stop_client(self._client, force=force) or 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def ask_sdk(prompt, *, turn_options=None, **kwargs):
    """One-shot SDK turn; ``kwargs`` configure :class:`SDKSession`."""
    with SDKSession(**kwargs) as session:
        response: Final = session.ask(prompt, **(turn_options or {}))
    # App-server EOF drains native capture before the one-shot result escapes.
    response.turns = _load_capture(response.capture_path)
    response.exit_status = session._exit_status
    return response


def ask_many_sdk(prompt, n=1, **kwargs):
    """Sequential independent SDK threads, matching ``pycodex.ask_many``."""
    return [ask_sdk(prompt, **kwargs) for _ in range(n)]
