"""Queue harness notifications through agy's native PreInvocation hook.

This module is deliberately stdlib-only: agy executes this file in a separate
Python process. A notification is a native ``CortexStepSystemMessage`` delivered
before the target conversation's next model invocation. It neither wakes an idle
agent nor changes the provider's system prompt.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import stat
import sys
import tempfile
import time


CHANNEL_ENV = "TASKSOLVER_SYSTEM_MESSAGE_CHANNEL"
MAX_MESSAGE_BYTES = 256 * 1024
MAX_BATCH_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 4 * 1024 * 1024
CONFIG_RETRIES = 5
_UNCHECKED = object()


class _ConfigChanged(RuntimeError):
    pass


def _stat_key(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _config_snapshot(path):
    """Read bytes and identity together, rejecting changes during the read."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode):
        raise ValueError("refusing to replace a symlinked hooks.json")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read()
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except FileNotFoundError as error:
        raise _ConfigChanged("hooks.json changed while being read") from error
    if not (_stat_key(before) == _stat_key(opened) == _stat_key(after) == _stat_key(current)):
        raise _ConfigChanged("hooks.json changed while being read")
    return _stat_key(current), raw


def _check_config(path, expected):
    if _config_snapshot(path) != expected:
        raise _ConfigChanged("hooks.json changed before replacement")


def _retry_config(update):
    for attempt in range(CONFIG_RETRIES):
        try:
            return update()
        except _ConfigChanged:
            if attempt + 1 == CONFIG_RETRIES:
                raise


@contextmanager
def _lock(path):
    with open(path, "a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _load(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _write(path, value, *, expected=_UNCHECKED):
    """Replace a complete JSON document; readers never see a partial write."""
    path = Path(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not _UNCHECKED:
            # Editors do not share our flock. Merge again when their changes are
            # detected; an uncooperative write after this check can still race.
            _check_config(path, expected)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _conversation(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("conversation_id must be a nonempty string of at most 256 characters")
    return value


def _message(text, conversation_id):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("system message must be a nonempty string")
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError(f"system message exceeds {MAX_MESSAGE_BYTES} UTF-8 bytes")
    if len(_batch_json([_step_json(text)]).encode("utf-8")) > MAX_BATCH_BYTES:
        raise ValueError(f"system message exceeds {MAX_BATCH_BYTES} encoded hook-output bytes")
    target = _conversation(conversation_id, optional=True)
    return text, target


def _step_json(text):
    # agy 1.4.11's ScrubInternalFields panics on the otherwise valid metadata
    # map[string]string. Keep IDs local; agy attributes the step to our hook.
    return json.dumps({"systemMessage": {"systemMessage": text}}, ensure_ascii=False)


def _batch_json(steps):
    """Encode already serialized steps, including all separators and the newline."""
    return '{"injectSteps": [' + ", ".join(steps) + "]}\n" if steps else "{}\n"


class SystemMessageChannel:
    """One process-scoped queue, usable before startup or during ``Session.ask``.

    ``send`` returns a message ID. ``status`` distinguishes pending messages from
    claimed ones and output emitted by a hook; emission is not an insertion ack.
    A callback crash after claiming is not retried automatically, because its
    stdout may already have reached agy. ``close`` removes the private queue.
    """

    def __init__(self, workspace, *, conversation_id=None):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("system-message workspace must be an existing directory")
        conversation_id = _conversation(conversation_id, optional=True)
        self._token = secrets.token_hex(24)
        self._key = "tasksolver-system-" + self._token
        self._queue = Path(tempfile.mkdtemp(prefix="tasksolver-system-"))
        self._queue.chmod(0o700)
        (self._queue / "messages").mkdir(mode=0o700)
        _write(self._queue / "state.json", {
            "main_conversation_id": conversation_id,
        })
        self._closed = False
        self._started = False
        self._spec = None
        self._config = self.workspace / ".agents" / "hooks.json"
        lock_root = Path(tempfile.gettempdir()) / f"tasksolver-hook-locks-{os.getuid()}"
        lock_root.mkdir(mode=0o700, exist_ok=True)
        if lock_root.is_symlink() or lock_root.stat().st_uid != os.getuid():
            self.close()
            raise ValueError("unsafe system-message hook lock directory")
        digest = hashlib.sha256(os.fsencode(self.workspace)).hexdigest()
        self._config_lock = lock_root / (digest + ".lock")
        self._registration = lock_root / (digest + ".json")

    def _check_open(self):
        if self._closed:
            raise RuntimeError("system-message channel is closed")

    def _read_config(self):
        snapshot = _config_snapshot(self._config)
        value = json.loads(snapshot[1]) if snapshot is not None else {}
        if not isinstance(value, dict):
            raise ValueError("hooks.json must contain an object")
        return value, snapshot

    def _registrations(self, config, snapshot):
        registry = _load(self._registration) if self._registration.exists() else {}
        entries = [key for key in registry.get("entries", []) if key in config]
        if not entries:
            return {"entries": [], "created_config": snapshot is None,
                    "created_agents": not self._config.parent.exists()}
        registry["entries"] = entries
        return registry

    def start(self):
        """Register a native hook and return the environment for this agy child."""
        self._check_open()
        if self._started:
            return {CHANNEL_ENV: self._token}
        command = shlex.join([
            "/usr/bin/env", "-u", "PYTHONHOME", "-u", "PYTHONPATH",
            sys.executable, "-I", "-S", str(Path(__file__).resolve()),
            "--channel", str(self._queue), "--token", self._token,
        ])
        self._spec = {"PreInvocation": [{"type": "command", "command": command,
                                          "timeout": 10}]}
        def register():
            config, snapshot = self._read_config()
            if self._key in config:
                raise RuntimeError("system-message hook name collision")
            before = _load(self._registration) if self._registration.exists() else None
            registry = self._registrations(config, snapshot)
            created_agents = not self._config.parent.exists()
            self._config.parent.mkdir(parents=True, exist_ok=True)
            config[self._key] = self._spec
            registry["entries"].append(self._key)
            try:
                _write(self._registration, registry)
                _write(self._config, config, expected=snapshot)
            except BaseException:
                if before is not None:
                    _write(self._registration, before)
                elif self._registration.exists():
                    self._registration.unlink()
                if created_agents:
                    try:
                        self._config.parent.rmdir()
                    except OSError:
                        pass
                raise
        with _lock(self._config_lock):
            _retry_config(register)
        self._started = True
        return {CHANNEL_ENV: self._token}

    def bind_main(self, conversation_id):
        """Supply the actual main conversation ID, including resumed sessions."""
        self._check_open()
        conversation_id = _conversation(conversation_id)
        with _lock(self._queue / ".lock"):
            state = _load(self._queue / "state.json")
            old = state.get("main_conversation_id")
            if old is not None and old != conversation_id:
                raise ValueError("system-message channel is already bound to another conversation")
            state["main_conversation_id"] = conversation_id
            _write(self._queue / "state.json", state)

    @property
    def main_conversation_id(self):
        self._check_open()
        with _lock(self._queue / ".lock"):
            return _load(self._queue / "state.json").get("main_conversation_id")

    def send(self, text, *, conversation_id=None):
        """Queue a notification for the main agent or a specific conversation ID."""
        self._check_open()
        text, target = _message(text, conversation_id)
        message_id = secrets.token_hex(16)
        with _lock(self._queue / ".lock"):
            _write(self._queue / "messages" / (message_id + ".json"), {
                "id": message_id, "text": text, "target": target,
                "state": "pending", "created_ns": time.time_ns(),
            })
        return message_id

    def status(self, message_id):
        """Return queue/emission state without claiming successful native insertion."""
        self._check_open()
        if not isinstance(message_id, str) or len(message_id) != 32 or any(
                c not in "0123456789abcdef" for c in message_id):
            raise ValueError("invalid system-message ID")
        with _lock(self._queue / ".lock"):
            path = self._queue / "messages" / (message_id + ".json")
            if not path.exists():
                raise KeyError(message_id)
            value = _load(path)
        return {k: value[k] for k in ("id", "state", "target", "conversation_id", "error")
                if k in value}

    def close(self):
        if self._closed:
            return
        try:
            if self._started:
                with _lock(self._config_lock):
                    registry = (_load(self._registration) if self._registration.exists()
                                else {"entries": []})

                    def unregister():
                        config, snapshot = self._read_config()
                        # Preserve a user edit to even our entry; never delete by name alone.
                        if config.get(self._key) == self._spec:
                            del config[self._key]
                            if not config and registry.get("created_config"):
                                _check_config(self._config, snapshot)
                                self._config.unlink()
                            else:
                                _write(self._config, config, expected=snapshot)
                            if registry.get("created_agents"):
                                try:
                                    self._config.parent.rmdir()
                                except OSError:
                                    pass
                    conflicted = False
                    try:
                        _retry_config(unregister)
                    except _ConfigChanged:
                        conflicted = True
                        raise
                    finally:
                        if not conflicted:
                            registry["entries"] = [key for key in registry["entries"]
                                                   if key != self._key]
                            if registry["entries"]:
                                _write(self._registration, registry)
                            elif self._registration.exists():
                                self._registration.unlink()
        finally:
            self._closed = True
            shutil.rmtree(self._queue)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _deliver(queue, payload, output):
    """Claim and emit a batch while holding the queue lock across the stdout flush."""
    conversation = _conversation(payload.get("conversationId"))
    parent = payload.get("parentConversationId")
    root = payload.get("rootConversationId")
    with _lock(queue / ".lock"):
        state_path = queue / "state.json"
        state = _load(state_path)
        main = state.get("main_conversation_id")
        if main is None:
            if root:
                main = _conversation(root)
            elif not parent:
                # A main model invocation precedes any subagent it creates. Some
                # CLI versions scrub parent/root IDs from external hook payloads.
                main = conversation
            if main is not None:
                state["main_conversation_id"] = main
                _write(state_path, state)
        pending = []
        for path in (queue / "messages").glob("*.json"):
            message = _load(path)
            if message.get("state") != "pending":
                continue
            target = message.get("target") or main
            if target == conversation:
                pending.append((path, message))
        pending.sort(key=lambda pair: (pair[1]["created_ns"], pair[1]["id"]))
        batch, steps = [], []
        size = len(_batch_json([""]).encode("utf-8"))
        for path, message in pending:
            try:
                text, _ = _message(message["text"], message["target"])
            except (ValueError, KeyError) as error:
                message.update(state="failed", error=str(error))
                _write(path, message)
                continue
            step = _step_json(text)
            step_size = len(step.encode("utf-8")) + (2 if steps else 0)
            if size + step_size > MAX_BATCH_BYTES:
                break
            size += step_size
            steps.append(step)
            batch.append((path, message))
        encoded = _batch_json(steps)
        for path, message in batch:
            message.update(state="claimed", conversation_id=conversation)
            _write(path, message)
        # On a broken pipe or crash the state stays claimed: it is not safe to
        # automatically repeat content that may already have reached the CLI.
        output.write(encoded)
        output.flush()
        for path, message in batch:
            message["state"] = "emitted"
            _write(path, message)


def _main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    if os.environ.get(CHANNEL_ENV) != args.token:
        print("{}")
        return 0
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("hook input is too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        _deliver(Path(args.channel), payload, sys.stdout)
    except (OSError, ValueError, KeyError) as error:
        print(f"tasksolver system-message hook: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
