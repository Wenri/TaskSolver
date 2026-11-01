"""Authenticated native system-message smoke test (makes three model turns).

Run after rebuilding tasksolver:
    pixi run python test_scripts/test_agy_system_messages_live.py

No credentials or native artifacts means a failure, never a passing skip. The
proof checks native observations and actual outgoing requests; model obedience
is not an assertion. A scoped conversation store preserves the user's sessions.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from pyagy import Session, ask, prepare_scoped_home


def check_message(response, marker):
    observed = [m for m in response.system_messages if marker in m["text"]]
    assert observed, f"missing native system-message observation for {marker}"
    assert all(not m["truncated"] for m in observed), "unexpected capture truncation"
    requests = [t["request_full"]["request"] for t in response.turns
                if t.get("request_full", {}).get("request")]
    parts = [p.get("text", "") for r in requests for c in r.get("contents", [])
             for p in c.get("parts", [])]
    assert any(marker in p and "<SYSTEM_MESSAGE>" in p for p in parts), \
        f"notification missing from model request for {marker}"
    assert not any(marker in json.dumps(r.get("systemInstruction", {})) for r in requests), \
        "notification unexpectedly changed the provider system prompt"
    return {"observations": len(observed),
            "sources": sorted({m["capture_source"] for m in observed}),
            "native_and_wire_verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = args.output_dir or Path(tempfile.mkdtemp(prefix="agy-system-api-live-"))
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    state = root / "state"
    prepare_scoped_home(str(state), link_global_config=False)
    options = dict(workspace=str(workspace), data_dir=str(state), timeout=180,
                   extra_env={key: None for key in os.environ if key.startswith("SSH_")})
    prompt = "Acknowledge the latest harness notification briefly. Do not use tools."
    proof = {}
    response = ask(prompt, system_messages=["ONE_SHOT_NOTIFICATION_83542"], **options)
    assert response.exit_status == 0, response.transcript[-2000:]
    proof["one_shot"] = check_message(response, "ONE_SHOT_NOTIFICATION_83542")
    assert not (workspace / ".agents" / "hooks.json").exists()

    # No parent capture-file reader: these observations must arrive over the
    # embedded worker's live result channel, including a later Session turn.
    with Session(capture=False, idle=3, **options) as session:
        first_id = session.send_system_message("SESSION_FIRST_NOTIFICATION_53721")
        first = session.ask(prompt)
        proof["session_first"] = check_message(first, "SESSION_FIRST_NOTIFICATION_53721")
        assert session.system_message_status(first_id)["state"] == "emitted"
        second_id = session.send_system_message("SESSION_SECOND_NOTIFICATION_94317",
                                                conversation_id=session.conversation_id)
        assert session.system_message_status(second_id)["state"] == "pending"
        second = session.ask(prompt)
        proof["session_second"] = check_message(second, "SESSION_SECOND_NOTIFICATION_94317")
        assert session.system_message_status(second_id)["state"] == "emitted"
        assert not any("SESSION_SECOND_NOTIFICATION_94317" in m["text"]
                       for m in first.system_messages), "earlier response mutated"
        assert first.capture_path is None and second.capture_path is None
    assert not (workspace / ".agents" / "hooks.json").exists()
    proof["hooks_cleaned"] = True
    (root / "proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps({"output_dir": str(root), **proof}, indent=2))


if __name__ == "__main__":
    main()
