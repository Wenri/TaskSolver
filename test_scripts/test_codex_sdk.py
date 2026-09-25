#!/usr/bin/env python3
"""Offline official-SDK protocol, capture, timeout and cleanup checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "codex", ROOT / "codex/vendor/sdk/python/src"):
    sys.path.insert(0, str(directory))

from openai_codex import LocalImageInput, TextInput
from openai_codex.errors import InvalidRequestError
from pycodex import SDKSession, ask_sdk

STUB: Final = r'''
import json, os, signal, subprocess, sys, time
mode = os.environ.get("STUB_MODE", "normal")
thread_id = "0199aabb-1234-7123-8123-123456789abc"
turn_n = 0
log = os.environ["STUB_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                       "home": os.environ.get("CODEX_HOME"),
                       "wire": os.environ.get("WIRE_ENABLE"),
                       "pythonhome": os.environ.get("PYTHONHOME"),
                       "pid": os.getpid(), "pgid": os.getpgrp()}) + "\n")
if mode in ("startup-hang", "turn-hang", "write-hang"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                             stdin=subprocess.DEVNULL)
    with open(os.environ["STUB_CHILD"], "w") as f:
        f.write(str(child.pid))

def send(obj):
    print(json.dumps(obj), flush=True)

def notify(method, params):
    send({"method": method, "params": params})

for line in sys.stdin:
    msg = json.loads(line)
    with open(log, "a") as f:
        f.write(json.dumps(msg) + "\n")
    method, params = msg["method"], msg.get("params", {})
    if "id" not in msg:
        continue
    if method == "initialize":
        if mode == "startup-hang":
            time.sleep(300)
        send({"id": msg["id"], "result": {"userAgent": "codex-cli/0.157.0"}})
    elif method == "account/login/start":
        send({"id": msg["id"], "result": {"type": "apiKey"}})
    elif method in ("thread/start", "thread/resume"):
        if mode == "rpc-error":
            send({"id": msg["id"], "error": {"code": -32600, "message": "bad thread"}})
            continue
        thread_id = params.get("threadId", thread_id)
        thread = {"id": thread_id, "sessionId": thread_id, "cliVersion": "0.157.0",
                  "createdAt": 1, "updatedAt": 1, "cwd": os.getcwd(),
                  "ephemeral": False, "modelProvider": "mock", "preview": "",
                  "source": "cli", "status": {"type": "idle"}, "turns": []}
        result = {"thread": thread, "model": "mock-model", "modelProvider": "mock",
                  "cwd": os.getcwd(), "approvalPolicy": "never", "approvalsReviewer": "user",
                  "sandbox": {"type": "readOnly"}}
        send({"id": msg["id"], "result": result})
        if mode == "write-hang":
            time.sleep(300)
    elif method == "turn/start":
        turn_n += 1
        turn_id = "turn-" + str(turn_n)
        turn = {"id": turn_id, "status": "inProgress", "items": []}
        send({"id": msg["id"], "result": {"turn": turn}})
        if mode == "turn-hang":
            time.sleep(300)
        if mode == "exit":
            sys.exit(9)
        if mode in ("failed-turn", "interrupted"):
            turn.update(status="failed" if mode == "failed-turn" else "interrupted",
                        error={"message": "mock failed", "codexErrorInfo": None})
            notify("turn/completed", {"threadId": thread_id, "turn": turn})
            continue
        answer = "answer-" + str(turn_n)
        usage = {"inputTokens": 10, "cachedInputTokens": 2, "outputTokens": 4,
                 "reasoningOutputTokens": 1, "totalTokens": 14}
        if os.environ.get("WIRE_ENABLE") == "1":
            with open(os.environ["WIRE_CAPTURE"], "a") as f:
                f.write(json.dumps({"kind": "codex_turn", "text": answer,
                    "request": {"model": "mock-model"},
                    "usage": {"input_tokens": 10, "cached_input_tokens": 2,
                              "output_tokens": 4, "reasoning_output_tokens": 1,
                              "total_tokens": 14}}) + "\n")
        item = {"type": "agentMessage", "id": "msg-" + str(turn_n),
                "phase": "final_answer", "text": answer}
        notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                "completedAtMs": 1, "item": {"type": "reasoning", "id": "reason-1",
                "summary": ["A synthetic summary."], "content": []}})
        notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                                   "item": item, "completedAtMs": 1})
        notify("thread/tokenUsage/updated", {"threadId": thread_id, "turnId": turn_id,
                "tokenUsage": {"last": usage, "total": usage}})
        turn.update(status="completed", items=[item])
        notify("turn/completed", {"threadId": thread_id, "turn": turn})
'''


def running(pid):
    path: Final = Path(f"/proc/{pid}/stat")
    return path.exists() and path.read_text().split(") ", 1)[1][0] != "Z"


def main():
    with tempfile.TemporaryDirectory(prefix="tasksolver-sdk-test-") as directory:
        root: Final = Path(directory)
        stub: Final = root / "codex"
        stub.write_text(f"#!{sys.executable}\n" + STUB)
        stub.chmod(0o755)
        workspace: Final = root / "workspace"
        workspace.mkdir()
        home: Final = root / "home"
        home.mkdir()
        log: Final = root / "calls.jsonl"
        child_file: Final = root / "child.pid"
        # Explicit module roots also reach sdk_process without installing a wheel.
        env: Final = {"STUB_LOG": str(log), "STUB_CHILD": str(child_file),
                      "OPENAI_API_KEY": "", "CODEX_API_KEY": "", "CODEX_ACCESS_TOKEN": "",
                      "PYTHONPATH": os.pathsep.join(map(str, (ROOT, ROOT / "codex")))}
        options: Final = dict(codex_bin=str(stub), workspace=str(workspace),
                              codex_home=str(home), extra_env=env, timeout=5)
        response: Final = ask_sdk("hello", **options,
                                  mcp_servers={"demo": {"command": "python", "args": ["demo.py"]}},
                                  config_overrides=('model_reasoning_effort="low"',))
        assert response.text == "answer-1" and response.model == "mock-model"
        assert response.session_id == "0199aabb-1234-7123-8123-123456789abc"
        assert len(response.turns) == 1 and response.usage.total_tokens == 14
        assert response.sdk_result.final_response == response.text
        assert not response.timed_out and response.exit_status == 0
        records = [json.loads(line) for line in log.read_text().splitlines()]
        launched: Final = records[0]
        assert launched["pid"] == launched["pgid"]
        assert launched["cwd"] == str(workspace) and launched["home"] == str(home)
        assert "PYTHONHOME" in " ".join(launched["argv"])
        start: Final = next(r["params"] for r in records if r.get("method") == "thread/start")
        assert start["sandbox"] == "read-only" and start["approvalPolicy"] == "never"
        assert not running(launched["pid"])
        print("ok one-shot: official JSON-RPC, native capture, MCP/home/config, isolated process")

        with SDKSession(**options) as session:
            first: Final = session.ask("one")
            second: Final = session.ask([TextInput(text="two"), LocalImageInput(path="/tmp/image.png")],
                                        output_schema={"type": "object"})
            assert first.text == "answer-1" and second.text == "answer-2"
            assert len(first.turns) == len(second.turns) == 1
            assert first.session_id == second.session_id
            assert session.thread.id == session.session_id
            events: Final = list(session.thread.turn("stream").stream())
            assert events[-1].method == "turn/completed"
        records = [json.loads(line) for line in log.read_text().splitlines()]
        turns: Final = [r["params"] for r in records if r.get("method") == "turn/start"]
        assert turns[-2]["input"][1] == {"type": "localImage", "path": "/tmp/image.png"}
        assert turns[-2]["outputSchema"] == {"type": "object"}
        print("ok multi-turn: typed images, output schema, stream, per-turn capture")

        resumed: Final = ask_sdk("resume", **options, session_id=response.session_id, capture=False)
        assert resumed.session_id == response.session_id and resumed.turns == []
        assert resumed.usage.total_tokens == 14 and resumed.capture_path == ""
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert any(r.get("method") == "thread/resume" for r in records)
        print("ok resume and SDK token accounting with capture disabled")

        authenticated: Final = ask_sdk("key", **{**options,
            "extra_env": {**env, "OPENAI_API_KEY": "sk-synthetic-protocol-test"}},
            config_overrides=('cli_auth_credentials_store="file"',))
        assert authenticated.text == "answer-1"
        records = [json.loads(line) for line in log.read_text().splitlines()]
        login: Final = next(r for r in records if r.get("method") == "account/login/start")
        assert login["params"]["apiKey"] == "sk-synthetic-protocol-test"
        launch_with_key: Final = [r for r in records if "argv" in r][-1]
        assert launch_with_key["argv"][-4] == 'cli_auth_credentials_store="ephemeral"'
        print("ok environment API key uses SDK login with forced ephemeral storage")

        from pycodex import CodexModel
        model: Final = CodexModel(model="codex-sdk", workspace=str(workspace),
                                 sdk_options={"codex_bin": str(stub), "extra_env": env,
                                              "codex_home": str(home)})
        messages, metadata = model.ask({"prompt": "model"})
        assert messages[0]["content"] == "answer-1"
        assert metadata[0]["sdk_result"].final_response == "answer-1"
        assert metadata[0]["session_id"] and metadata[0]["turns"]
        assert metadata[0]["explicit_reasoning_output"] == "A synthetic summary."
        assert model.model is None
        print("ok TaskSolver contract and SDK metadata")

        from PIL import Image
        from tasksolver.common import ParsedAnswer, Question, TaskSpec

        class Answer(ParsedAnswer):
            def __init__(self, text):
                self.text = text

            @staticmethod
            def parser(text):
                return Answer(text)

            def __str__(self):
                return self.text

        model.task = TaskSpec("sdk", "Describe the image", Answer,
                              lambda *_: None, lambda *_: True)
        previous_cwd: Final = os.getcwd()
        os.chdir(root)
        try:
            answer, raw, attached, payload = model.run_once(
                Question([Image.new("RGB", (8, 8), "red"), "What color?"]))
        finally:
            os.chdir(previous_cwd)
        assert str(answer) == "answer-1" and raw["content"] == "answer-1"
        assert answer.explicit_reasoning_output == "A synthetic summary."
        assert answer.request_payload == payload and attached[0]["session_id"]
        assert len(payload["image_paths"]) == 1
        records = [json.loads(line) for line in log.read_text().splitlines()]
        image_turn: Final = [r["params"] for r in records if r.get("method") == "turn/start"][-1]
        assert image_turn["input"][1]["type"] == "localImage"
        assert Path(image_turn["input"][1]["path"]).is_absolute()
        assert Path(image_turn["input"][1]["path"]).is_file()
        legacy: Final = CodexModel(transport="exec")
        from pycodex.client import ask_many
        assert legacy._client_ask_many is ask_many
        print("ok default SDK: image TaskSpec four-tuple, reasoning attachment, exec opt-in")

        for mode in ("startup-hang", "turn-hang", "write-hang"):
            start_time: Final = time.monotonic()
            prompt = "x" * (8 * 1024 * 1024) if mode == "write-hang" else "timeout"
            result = ask_sdk(prompt, **{**options, "timeout": 0.5,
                             "extra_env": {**env, "STUB_MODE": mode}})
            assert result.timed_out and time.monotonic() - start_time < 6
            assert not running(int(child_file.read_text()))
        print("ok startup, turn and blocked-write deadlines reap MCP/tool descendants")

        for mode in ("rpc-error", "failed-turn", "interrupted", "exit"):
            try:
                ask_sdk("error", **{**options, "extra_env": {**env, "STUB_MODE": mode}})
            except Exception as exc:
                if mode == "rpc-error":
                    assert isinstance(exc, InvalidRequestError)
                if mode == "failed-turn":
                    assert "mock failed" in str(exc)
                if mode == "interrupted":
                    assert "interrupted" in str(exc)
            else:
                raise AssertionError(f"{mode} was silently accepted")
        print("ok RPC, failed-turn and unexpected-exit errors propagate")

        try:
            SDKSession(**options, session_id="x", continue_latest=True)
        except ValueError:
            pass
        else:
            raise AssertionError("ambiguous resume options were accepted")
        try:
            SDKSession(**options, continue_latest=True)
        except ValueError:
            pass
        else:
            raise AssertionError("missing latest session was silently replaced")
        print("ok invalid and missing resume targets fail explicitly")
    print("All Codex SDK checks passed")


if __name__ == "__main__":
    main()
