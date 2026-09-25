#!/usr/bin/env python3
"""Offline native SDK/app-server + instrumented capture smoke (requires built Codex).

Uses only a localhost Responses fixture, an empty temporary CODEX_HOME and
synthetic replies. No external provider requests or credential reads.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile
from typing import Final
from unittest.mock import patch

ROOT: Final = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "codex", ROOT / "codex/vendor/sdk/python/src",
                  ROOT / "codex/vendor/sdk/python/tests"):
    sys.path.insert(0, str(directory))

from app_server_harness import AppServerHarness
from pycodex import SDKSession, ask_sdk
from pycodex._env import CODEX_BIN


def main():
    with tempfile.TemporaryDirectory(prefix="tasksolver-sdk-native-") as directory:
        with AppServerHarness(Path(directory)) as harness:
            env: Final = {
                "CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG": "1",
                "OPENAI_API_KEY": "", "OPENAI_BASE_URL": "",
                "CODEX_API_KEY": "", "CODEX_ACCESS_TOKEN": "", "HOME": directory,
                "PYTHONPATH": os.pathsep.join(map(str, (ROOT, ROOT / "codex"))),
                "RUST_LOG": "warn",
            }
            kwargs: Final = dict(
                codex_bin=CODEX_BIN, codex_home=str(harness.codex_home),
                workspace=str(harness.workspace), extra_env=env, timeout=30)
            harness.responses.enqueue_assistant_message("First SDK answer", response_id="native-1")
            first: Final = ask_sdk("first question", **kwargs)
            assert first.text == "First SDK answer" and not first.timed_out, first
            assert first.session_id and first.request and first.turns
            assert first.request["model"] == "mock-model"
            assert first.usage.total_tokens > 0
            print("ok native SDK/app-server: final response, native request/response capture, usage")

            harness.responses.enqueue_assistant_message("Resumed SDK answer", response_id="native-2")
            harness.responses.enqueue_assistant_message("Third SDK answer", response_id="native-3")
            with SDKSession(session_id=first.session_id, **kwargs) as session:
                second: Final = session.ask("second question")
                third: Final = session.ask("third question")
                assert second.text == "Resumed SDK answer" and third.text == "Third SDK answer"
                assert first.session_id == second.session_id == third.session_id
                assert second.turns and third.turns
                assert session.history()
            requests: Final = harness.responses.requests()
            assert len(requests) == 3, len(requests)
            assert requests[-1].message_input_texts("user")[-3:] == [
                "first question", "second question", "third question"]
            print("ok native resume + persistent SDK context and per-turn capture")

    with tempfile.TemporaryDirectory(prefix="tasksolver-sdk-native-auth-") as directory:
        with AppServerHarness(Path(directory), requires_openai_auth=True) as harness:
            auth_env: Final = {
                "CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG": "1",
                "OPENAI_API_KEY": "sk-synthetic-sdk-auth", "CODEX_API_KEY": "",
                "CODEX_ACCESS_TOKEN": "", "OPENAI_BASE_URL": "", "HOME": directory,
                "PYTHONPATH": os.pathsep.join(map(str, (ROOT, ROOT / "codex"))),
                "RUST_LOG": "warn",
            }
            auth_kwargs: Final = dict(codex_home=str(harness.codex_home),
                                     workspace=str(harness.workspace), extra_env=auth_env, timeout=30)
            auth_file: Final = harness.codex_home / "auth.json"
            harness.responses.enqueue_assistant_message("API key answer", response_id="auth-1")
            authenticated: Final = ask_sdk("use the supplied key", **auth_kwargs)
            assert authenticated.text == "API key answer" and not auth_file.exists()
            assert harness.responses.requests()[-1].header("authorization") == (
                "Bearer sk-synthetic-sdk-auth")

            preserved: Final = json.dumps({"OPENAI_API_KEY": "sk-synthetic-stored-login"})
            auth_file.write_text(preserved)
            harness.responses.enqueue_assistant_message("Preserved login", response_id="auth-2")
            # Exercise inherited OPENAI_API_KEY too, without inheriting any real credential.
            inherited_env: Final = {key: value for key, value in auth_env.items()
                                    if key != "OPENAI_API_KEY"}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-synthetic-inherited-auth"}):
                resumed: Final = ask_sdk("keep stored credentials intact",
                    **{**auth_kwargs, "extra_env": inherited_env}, session_id=authenticated.session_id,
                    config_overrides=('cli_auth_credentials_store="file"',))
            assert resumed.text == "Preserved login"
            assert auth_file.read_text() == preserved
            assert harness.responses.requests()[-1].header("authorization") == (
                "Bearer sk-synthetic-inherited-auth")
            print("ok native SDK API-key auth: Authorization sent, stored login untouched")
    print("All native Codex SDK checks passed")


if __name__ == "__main__":
    main()
