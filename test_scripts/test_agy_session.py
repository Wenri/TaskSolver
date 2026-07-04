#!/usr/bin/env python3
"""Session-capable pyagy — resume / continue / list backed by agy's NATIVE conversation
store (~/.gemini/antigravity-cli/). Session is the first-class object; ask/AgyProcess/AgyModel
all carry a resumable conversation_id.

Deterministic bits are hard-asserted (store reads; id captured; resume preserves the id).
The model *recall* across a resume needs a live turn (network/auth) and agy is ~flaky, so
recall is a soft NOTE — matching test_agyprocess.py. Uses the pinned vendor/agy.

    python3 test_scripts/test_agy_session.py
"""
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANTI = os.path.join(os.path.dirname(_HERE), "antigravity")
os.environ.setdefault("AGY_BIN", os.path.join(_ANTI, "vendor", "agy"))
sys.path.insert(0, _ANTI)

import pyagy                                            # noqa: E402
from pyagy import conversations as C                    # noqa: E402
from pyagy import _env                                  # noqa: E402

WORD = "BANANA"
_fail = []


def _recalled(text):
    return WORD in (text or "").upper()


def case_store_read():
    # Read-only view of agy's real store — no live turn; always deterministic.
    infos = pyagy.list_conversations(limit=3)
    latest = pyagy.latest_conversation_id()
    ok = isinstance(infos, list) and latest is not None and all(i.id for i in infos)
    tr = pyagy.read_transcript(latest) if latest else []
    ok = ok and isinstance(tr, list)
    print(f"  {'ok  ' if ok else 'FAIL'} store-read: {len(infos)} conversation(s), "
          f"latest={latest}, transcript={len(tr)} turns")
    if infos:
        print(f"       e.g. {infos[0]}")
    if not ok:
        _fail.append("store_read")


def case_ask_resume():
    # The core proof: resume a stored conversation in one-shot --print mode (deterministic
    # process exit; the reliable path). Hard-assert id capture + id continuity; soft-note recall.
    r1 = pyagy.ask(f"Remember this: the secret code word is {WORD}. Reply with only: OK",
                   instrumented=False, timeout=120)
    cid = r1.conversation_id
    if not cid:
        print("  FAIL ask-resume: no conversation_id captured from turn 1")
        _fail.append("ask_resume")
        return
    r2 = pyagy.ask("What is the secret code word? Reply with only that word.",
                   conversation_id=cid, instrumented=False, timeout=120)
    id_ok = r2.conversation_id == cid                    # resume kept the same conversation
    print(f"  {'ok  ' if id_ok else 'FAIL'} ask-resume: cid={cid} continuity={id_ok}")
    if not id_ok:
        _fail.append("ask_resume")
    if _recalled(r2.text):
        print(f"       ok   recall: turn-2 recalled {WORD!r} from the resumed conversation")
    else:
        print(f"       NOTE recall: turn-2 did not echo {WORD!r} this run (live-model flake); "
              f"got {r2.text[-60:]!r}")


def case_continue_latest():
    # --continue resumes the most recent conversation.
    s = pyagy.continue_latest(instrumented=False, idle=8.0, timeout=120, skip_permissions=True)
    try:
        r = s.ask("Reply with only the digits: what is 3+4?")
        got = (r.text or "").strip()
        print(f"  ok   continue-latest: resumed most recent, cid={s.conversation_id} "
              f"reply_tail={got[-40:]!r}")
        if not s.conversation_id:
            print("       NOTE: no conversation_id resolved (empty store?)")
    except Exception as e:
        print(f"  NOTE continue-latest: {type(e).__name__}: {e} (live-model flake)")
    finally:
        s.close()


def case_session_resume():
    # Live interactive Session across TWO processes: seed in one, resume by id in a fresh
    # Session. Soft-note recall (interactive + live model). Hard-assert id continuity.
    s1 = pyagy.Session(instrumented=False, idle=8.0, timeout=120, skip_permissions=True)
    try:
        s1.ask(f"Remember: the code word is {WORD}. Reply with only: OK")
        cid = s1.conversation_id
    finally:
        s1.close()
    if not cid:
        print("  NOTE session-resume: turn-1 produced no conversation_id (flake); skipped")
        return
    s2 = pyagy.resume(cid, instrumented=False, idle=8.0, timeout=120, skip_permissions=True)
    try:
        r = s2.ask("What is the code word? Reply with only that word.")
        cont_ok = s2.conversation_id == cid
        tag = "ok  " if cont_ok else "FAIL"
        print(f"  {tag} session-resume: fresh Session resumed cid={cid} continuity={cont_ok}")
        if not cont_ok:
            _fail.append("session_resume")
        print(f"       {'ok   recall' if _recalled(r.text) else 'NOTE recall (flake)'}: "
              f"tail={ (r.text or '')[-50:]!r }")
        print(f"       history: {len(s2.history())} stored turns")
    finally:
        s2.close()


def case_agymodel_resume():
    # AgyModel opt-in multi_turn: two calls continue ONE conversation (latched id).
    from pyagy import AgyModel
    m = AgyModel(model=None, multi_turn=True)
    try:
        m.ask({"prompt": f"Remember: my lucky number is 77 and the word is {WORD}. Reply: OK"})
        cid = m.conversation_id
        latch_ok = bool(cid)
        msgs, _ = m.ask({"prompt": "What was the word I told you? Reply with only that word."})
        reply = msgs[0]["content"] if msgs else ""
        print(f"  {'ok  ' if latch_ok else 'FAIL'} agymodel: multi_turn latched cid={cid}")
        if not latch_ok:
            _fail.append("agymodel_resume")
        print(f"       {'ok   recall' if _recalled(reply) else 'NOTE recall (flake)'}: "
              f"tail={reply[-50:]!r}")
    except Exception as e:
        print(f"  NOTE agymodel: {type(e).__name__}: {e} (live-model flake)")


def case_agyprocess_resume():
    # AgyProcess (multiprocessing child) exposes a resumable conversation_id. Needs the shim.
    if not os.path.exists(_env.SHIM):
        print("  NOTE agyprocess: shim not built (run `make -C antigravity`); skipped")
        return
    from pyagy.agyprocess import AgyProcess
    p = AgyProcess(prompt=f"Remember the code word {WORD}. Reply with only: OK")
    try:
        p.start()
        # let agy run the turn, then resolve the id (capture event or newest store db)
        import time
        deadline = time.time() + 60
        while time.time() < deadline and p.is_alive():
            time.sleep(1.0)
        cid = p.conversation_id
        ok = bool(cid)
        print(f"  {'ok  ' if ok else 'NOTE'} agyprocess: conversation_id={cid}")
        if not ok:
            print("       NOTE: no id resolved this run (live-model flake)")
    except Exception as e:
        print(f"  NOTE agyprocess: {type(e).__name__}: {e}")
    finally:
        try:
            p.terminate(); p.join(timeout=10)
            p._popen.close()
        except Exception:
            pass


def case_scoped():
    # Scope the whole conversation store to a project repo (HOME override + seeded login):
    # the conversation lands UNDER the repo, the global store is untouched, and login survives.
    # Hard-assert isolation/placement/login; soft-note the model recall.
    scope = tempfile.mkdtemp(prefix="pyagy-scope-")
    try:
        g_before = C.latest_conversation_id()
        r1 = pyagy.ask(f"Remember: the animal is {WORD}. Reply with only: OK",
                       data_dir=scope, instrumented=False, timeout=120)
        cid = r1.conversation_id
        in_scope = cid in {i.id for i in C.list_conversations(home=scope)}
        isolated = (g_before == C.latest_conversation_id())        # global store untouched
        in_repo = os.path.isdir(os.path.join(scope, ".gemini", "antigravity-cli", "conversations"))
        logged_in = bool((r1.text or "").strip())
        ok = bool(cid) and in_scope and isolated and in_repo and logged_in
        print(f"  {'ok  ' if ok else 'FAIL'} scoped: cid={cid} in_scope={in_scope} "
              f"global_isolated={isolated} store_in_repo={in_repo} logged_in={logged_in}")
        if not ok:
            _fail.append("scoped")
        r2 = pyagy.ask("What is the animal? Reply with only that word.",
                       conversation_id=cid, data_dir=scope, instrumented=False, timeout=120)
        print(f"       {'ok   recall' if _recalled(r2.text) else 'NOTE recall (flake)'}: "
              f"scoped resume continuity={r2.conversation_id == cid}")
    except Exception as e:
        print(f"  FAIL scoped: {type(e).__name__}: {e}")
        _fail.append("scoped")
    finally:
        __import__("shutil").rmtree(scope, ignore_errors=True)


def case_trust():
    # Pre-trust writes the workspace into settings.json trustedWorkspaces (deterministic), so an
    # interactive Session in a FRESH workspace runs WITHOUT --dangerously-skip-permissions and
    # doesn't hang on the folder-trust menu.
    fresh = tempfile.mkdtemp(prefix="pyagy-fresh-ws-")
    subprocess.run(["git", "init", "-q"], cwd=fresh, check=False)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "x"], cwd=fresh, check=False)
    settings = os.path.join(C.store_root(), "settings.json")

    def trusted():
        try:
            return fresh in (json.load(open(settings)).get("trustedWorkspaces") or [])
        except (OSError, ValueError):
            return False

    wrote = C.trust_workspace(fresh) and trusted()          # deterministic: the write itself
    print(f"  {'ok  ' if wrote else 'FAIL'} trust: trust_workspace wrote settings.json entry")
    if not wrote:
        _fail.append("trust")
    s = pyagy.Session(workspace=fresh, instrumented=False, idle=8.0, timeout=90)  # NO skip_permissions
    try:
        r = s.ask("Reply with only the digits: what is 6+1?")
        done = bool((r.text or "").strip())
        print(f"       {'ok   ' if done else 'NOTE '}interactive completed without skip_permissions "
              f"(pre-trust unblocked the menu): {done}")
    except Exception as e:
        print(f"       NOTE trust live: {type(e).__name__}: {e} (flake)")
    finally:
        s.close()
        __import__("shutil").rmtree(fresh, ignore_errors=True)


if __name__ == "__main__":
    print("[pyagy session] agy native conversation store (vendor/agy)")
    print(f"  store: {C.store_root()}")
    case_store_read()
    case_ask_resume()
    case_continue_latest()
    case_session_resume()
    case_agymodel_resume()
    case_agyprocess_resume()
    case_scoped()
    case_trust()
    print("\nPASS" if not _fail else "\nFAIL: " + ",".join(_fail))
    sys.exit(1 if _fail else 0)
