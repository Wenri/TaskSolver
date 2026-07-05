#!/usr/bin/env python3
"""Drive an `agy` session under a controlled PTY + the antigravity network hooks, for capture
experiments. agy is a TUI that wants a real terminal, so we run it under a pty we own; the
LD_PRELOAD shim captures the model traffic to the AGY_PROC_CAPTURE JSONL, and an in-agy worker
streams the decoded answer home over a Connection (collected by run()/ask()).

Two "outputs":
  * decoded answer  — the genai_turn/app_response objects the worker streams home (run()/ask()).
  * network capture — the request/response written to the AGY_PROC_CAPTURE JSONL (inspect the file).
The PTY transcript (agy's rendered output, ANSI-stripped) is on .transcript/.raw — useful for
auth/crash checks.

The PTY fork/pump, terminal-query handling, and instrumented env wiring live in `pyagy.AgyProcess`
over `_pty.PtyPopen`; this is a thin façade that takes the agy argv directly, kept as the CLI used
in capture experiments.

Usage:
    python3 agy_session.py --mode print --prompt "what is 2+2"
    python3 agy_session.py --mode interactive --prompt "what is 2+2" --send "and times 10?"

As a library:
    s = AgySession(capture="cap.jsonl", workdir="/tmp/ws")
    s.start(["--print", "what is 2+2"])
    turns = s.collect(timeout=120)      # decoded answer objects; s.transcript = the raw PTY
    s.close()
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANTIGRAVITY = os.path.join(os.path.dirname(_HERE), "antigravity")
if _ANTIGRAVITY not in sys.path:
    sys.path.insert(0, _ANTIGRAVITY)   # make `pyagy` importable without an install

from pyagy.agyprocess import AgyProcess   # noqa: E402
from pyagy._term import strip_ansi        # noqa: E402  (re-exported for callers)


class AgySession:
    """Instrumented agy under a PTY for capture experiments — a thin façade over
    :class:`AgyProcess` that takes the agy argv tail directly via ``start(args)``. Drive a one-shot
    ``--print`` turn with ``run()`` or a persistent ``--prompt-interactive`` turn with ``ask()``;
    both return the decoded answer objects the worker streams home. The PTY transcript is on
    ``.transcript`` / ``.raw`` (auth/crash checks); the capture JSONL is written to ``capture``.
    ``.proc`` is the underlying AgyProcess."""

    def __init__(self, agy=None, capture="agy-capture.jsonl",
                 log=None, workdir=None, extra_env=None, echo=False):
        self.agy = agy
        self.capture = os.path.abspath(capture)
        self.log = os.path.abspath(log) if log else None
        self.workdir = workdir or os.getcwd()
        self.extra_env = dict(extra_env or {})
        self.echo = echo
        self.proc = None                    # the AgyProcess, set on start()

    # --- back-compat shims over the underlying PtyPopen ------------------------
    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    @property
    def fd(self):
        return self.proc._popen.fd if self.proc else None

    @property
    def raw(self):
        return self.proc._popen.raw if self.proc else bytearray()

    @property
    def transcript(self):
        return self.proc.transcript if self.proc else ""

    def start(self, args):
        """Fork agy under a pty with the given argv tail (e.g. ``["--print", prompt]``)."""
        extra = dict(self.extra_env)
        if self.log:
            extra["AGY_PROC_LOG"] = self.log
        self.proc = AgyProcess(agy_bin=self.agy, agy_args=list(args), workdir=self.workdir,
                               capture=self.capture, extra_env=extra, echo=self.echo)
        self.proc.start()
        return self

    def collect(self, timeout=200.0):
        """One-shot: drive agy to completion, draining the PTY; return the decoded answer objects
        (the transcript is on ``.transcript`` / ``.raw``, the capture JSONL on ``capture``)."""
        return self.proc.collect(timeout=timeout)

    def ask(self, prompt=None, idle=4.0, timeout=120.0):
        """One persistent turn: submit ``prompt`` (or the prefill) and return its decoded objects."""
        return self.proc.ask(prompt, idle=idle, timeout=timeout)

    def send(self, data: bytes):
        self.proc.write(data)

    def send_line(self, text: str):
        self.proc.send_line(text)

    def close(self):
        if self.proc is not None:
            self.proc.close(interrupt=True)


def _summarize_capture(path):
    import collections
    import json
    if not os.path.exists(path):
        return "no capture file"
    c, b = collections.Counter(), collections.Counter()
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        c[r.get("kind")] += 1
        b[r.get("kind")] += r.get("len", r.get("body_len", 0))
    return "  ".join(f"{k}={c[k]}({b[k]}B)" for k in sorted(c))


def _answer(objs):
    return "\n".join(o.get("text", "") for o in objs if (o.get("text") or "").strip()).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["interactive", "print"], default="print")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--send", action="append", default=[], help="follow-up input (interactive; repeatable)")
    ap.add_argument("--idle", type=float, default=4.0)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--capture", default="agy-capture.jsonl")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--echo", action="store_true", help="mirror agy output to our stdout live")
    args = ap.parse_args()

    s = AgySession(capture=args.capture, workdir=args.workdir, echo=args.echo)
    if args.mode == "print":
        print(f"[agy_session] agy --print {args.prompt!r}", file=sys.stderr)
        s.start(["--print", args.prompt])
        print("\n===== answer =====\n" + (_answer(s.collect(timeout=args.timeout)) or "(no decoded turn)"))
    else:
        print(f"[agy_session] agy --prompt-interactive {args.prompt!r}", file=sys.stderr)
        s.start(["--prompt-interactive", args.prompt])
        print("\n===== turn 1 =====\n" + _answer(s.ask(None, idle=args.idle, timeout=args.timeout)))
        for i, follow in enumerate(args.send, 2):
            print(f"\n===== turn {i}: {follow!r} =====\n"
                  + _answer(s.ask(follow, idle=args.idle, timeout=args.timeout)))
    s.close()
    print("\n[agy_session] capture:", _summarize_capture(os.path.abspath(args.capture)), file=sys.stderr)


if __name__ == "__main__":
    main()
