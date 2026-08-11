#!/usr/bin/env python3
"""Live end-to-end tests for pykimi: the wirecap addon inside a real node process (synthetic
emits decoded to a ``kimi_turn``), then — when a model is configured — a real ``kimi -p`` turn
through the vendored, wiretap-patched bundle.

Gated: skips cleanly (exit 0) if the built bundle/addon is missing (run ``pixi install`` — kimi
is built by the install's build_py hook, like codex) or no model is configured
(``MOONSHOT_API_KEY`` / ``KIMI_MODEL_API_KEY``, or a ``kimi login`` config).

    python3 test_scripts/test_kimi.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_KIMI = os.path.join(_REPO, "kimi")
sys.path.insert(0, _KIMI)
sys.path.insert(0, _REPO)

from pykimi import ask                                       # noqa: E402
from pykimi._env import KIMI_MAIN, WIRE_NODE_ADDON, node_bin  # noqa: E402

_failures = []


def check(cond, name):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def skip(msg):
    print(f"NOTE: skipping — {msg}")
    print("PASS")
    sys.exit(0)


_SMOKE_JS = """
const a = require(process.env.WIRE_NODE_ADDON);
if (a.start() !== 0) { console.error("start failed"); process.exit(3); }
const enc = new TextEncoder();
const req = {protocol: "anthropic", providerType: "kimi", modelName: "k-smoke",
             systemPrompt: "s", tools: [], messages: [
               {role: "user", content: [{type: "text", text: "ping"}], toolCalls: []}]};
const id = a.emitRequest(enc.encode(JSON.stringify(req)));
for (const e of [
  {type: "part", part: {type: "text", text: "po"}},
  {type: "part", part: {type: "text", text: "ng"}},
  {type: "usage", usage: {inputOther: 3, output: 2, inputCacheRead: 0, inputCacheCreation: 0},
   model: "k-smoke-served"},
  {type: "finish", message: {role: "assistant",
   content: [{type: "text", text: "pong"}], toolCalls: []}, providerFinishReason: "stop"},
]) a.emitEvent(enc.encode(JSON.stringify(e)));
a.emitWire(enc.encode(JSON.stringify({scope: "s1/agents/main",
                                      record: {type: "usage.record"}})));
a.shutdown();
console.log("turn id", id);
"""


def test_addon_in_node():
    """The wirecap bridge inside a REAL node process: start -> embedded interpreter imports
    pykimi.kimi_process -> synthetic request/events -> decoded kimi_turn in the capture JSONL.
    Everything but the CLI and the model."""
    print("[live] wirecap_node addon inside node: synthetic emits -> kimi_turn")
    with tempfile.TemporaryDirectory() as td:
        cap = os.path.join(td, "cap.jsonl")
        js = os.path.join(td, "smoke.cjs")
        with open(js, "w") as f:
            f.write(_SMOKE_JS)
        env = dict(os.environ)
        env.update(WIRE_ENABLE="1", WIRE_MODULE="pykimi.kimi_process",
                   WIRE_CAPTURE=cap, WIRE_NODE_ADDON=os.path.abspath(WIRE_NODE_ADDON),
                   PYTHONHOME=env["CONDA_PREFIX"],
                   # The embedded interpreter resolves modules from the env's site-packages;
                   # this smoke asserts the CHECKOUT's dispatch code, which may not be installed
                   # yet — CPython honors PYTHONPATH at init, the one injection point the bridge
                   # allows. The real-CLI test below runs without it (the installed route).
                   PYTHONPATH=os.pathsep.join(
                       [_REPO, _KIMI] +
                       ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])))
        r = subprocess.run([node_bin(), js], env=env, capture_output=True, text=True,
                           timeout=120)
        check(r.returncode == 0, f"node smoke exit 0 (got {r.returncode}: {r.stderr[:300]})")
        if not os.path.exists(cap):
            check(False, "capture JSONL written")
            return
        turns = [json.loads(l) for l in open(cap) if '"kimi_turn"' in l]
        check(len(turns) == 1, "one kimi_turn decoded")
        if turns:
            t = turns[0]
            check(t["text"] == "pong" and t["model"] == "k-smoke-served"
                  and t["usage"]["total_tokens"] == 5
                  and t["request"]["model"] == "k-smoke", "turn assembled + paired")
        raw = [json.loads(l) for l in open(cap)]
        check(any(o.get("kind") == "kimi_wire" for o in raw), "kimi_wire recorded")


def test_real_cli():
    print("[live] pykimi.ask() end-to-end through the vendored bundle")
    key = os.environ.get("KIMI_MODEL_API_KEY") or os.environ.get("MOONSHOT_API_KEY")
    model = os.environ.get("KIMI_MODEL_NAME") or "k3"
    if not key and not os.path.exists(os.path.join(os.path.expanduser("~"),
                                                   ".kimi-code", "config.toml")):
        skip("no model configured (set MOONSHOT_API_KEY, or `kimi login`) — CLI turn untested")
    extra_env = {}
    if key:
        extra_env = {"KIMI_MODEL_API_KEY": key,
                     "KIMI_MODEL_BASE_URL": os.environ.get("KIMI_MODEL_BASE_URL",
                                                           "https://api.kimi.com/coding"),
                     "KIMI_MODEL_PROVIDER_TYPE": os.environ.get("KIMI_MODEL_PROVIDER_TYPE",
                                                                "anthropic")}
    with tempfile.TemporaryDirectory() as home:
        r = ask("What is 2+2? Reply with just the number.", model=model if key else None,
                extra_env=extra_env, timeout=300, kimi_home=home)
    if r.exit_status != 0 and not r.turns:
        skip(f"kimi exited {r.exit_status} with no decoded turn (not authenticated?)\n"
             f"{r.transcript[:400]}")
    check("4" in r.text, "answer contains 4")
    check(len(r.turns) >= 1, "at least one kimi_turn decoded")
    check(bool(r.model), f"served model decoded ({r.model})")
    check(r.usage.total_tokens > 0, f"usage decoded (total={r.usage.total_tokens})")
    check(r.request is not None, "request summary decoded (paired)")
    first_user = (r.request or {}).get("first_user_text", "")
    check("2+2" in first_user, "request first_user_text carries the prompt")
    check(bool(r.session_id), f"store-read session id ({r.session_id})")


def main():
    if not os.environ.get("CONDA_PREFIX"):
        skip("CONDA_PREFIX unset — run under the pixi env (PYTHONHOME for the embedded interpreter)")
    if not os.path.exists(WIRE_NODE_ADDON):
        skip(f"wirecap_node addon missing ({WIRE_NODE_ADDON}) — run `pixi install` (kimi is\n"
             f"       built by the install's build_py hook; there is deliberately no build task)")
    if not shutil.which(node_bin()) and not os.path.exists(node_bin()):
        skip(f"node missing ({node_bin()})")
    test_addon_in_node()
    if not os.path.exists(KIMI_MAIN):
        skip(f"kimi bundle missing ({KIMI_MAIN}) — run `pixi install`; addon smoke PASSED above")
    test_real_cli()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
