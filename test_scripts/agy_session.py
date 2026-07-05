#!/usr/bin/env python3
"""Drive an interactive `agy` session under a controlled PTY + the antigravity network
hooks. This is the "session" layer: agy is a TUI that wants a real terminal, so we
give it a pty we own — letting us read its rendered output and inject further input,
while the LD_PRELOAD hooks capture the model traffic (clean, ANSI-free) in parallel.

Two channels of "output":
  * PTY transcript  — what agy renders (ANSI-stripped here). Good for UI state.
  * network capture — the actual Gemini request/response (via crypto/tls hooks),
                      written to the AGY_PROC_CAPTURE JSONL. Cleaner for content.

The PTY fork/pump, ANSI/terminal-query handling, and instrumented env wiring now live in
`pyagy.AgyProcess` (plain-CLI mode) over `_pty.PtyPopen`; this is a thin façade that takes the
agy argv directly, kept here as the CLI used in capture experiments.

Usage:
    python3 agy_session.py --mode interactive --prompt "what is 2+2" \
        --send "and times 10?" --idle 4 --timeout 120
    python3 agy_session.py --mode print --prompt "what is 2+2"   # one-shot

As a library:
    s = AgySession(capture="cap.jsonl", workdir="/tmp/ws")
    s.start(["--prompt-interactive", "what is 2+2"])
    print(s.read_until_idle(idle=4, timeout=120))     # assistant's first answer
    s.send_line("and multiply by 10?")
    print(s.read_until_idle(idle=4, timeout=120))
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
    :class:`AgyProcess` (plain-CLI mode) that takes the agy argv tail directly via
    ``start(args)``. ``.proc`` is the underlying AgyProcess (use ``.proc.read_until_exit``
    for one-shot ``--print`` turns; ``.read_until_idle`` for interactive)."""

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

    def start(self, args):
        """Fork agy under a pty with the given argv tail (e.g. ``["--print", prompt]``)."""
        extra = dict(self.extra_env)
        if self.log:
            extra["AGY_PROC_LOG"] = self.log
        self.proc = AgyProcess(agy_bin=self.agy, agy_args=list(args), workdir=self.workdir,
                               capture=self.capture, extra_env=extra, echo=self.echo)
        self.proc.start()
        return self

    def read_until_idle(self, idle=3.0, timeout=120.0):
        return self.proc.read_until_idle(idle=idle, timeout=timeout)

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["interactive", "print"], default="interactive")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--send", action="append", default=[], help="follow-up input (repeatable)")
    ap.add_argument("--idle", type=float, default=4.0)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--capture", default="agy-capture.jsonl")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--echo", action="store_true", help="mirror agy output to our stdout live")
    ap.add_argument("--no-submit", action="store_true",
                    help="don't auto-press Enter after startup (interactive prefills the prompt)")
    args = ap.parse_args()

    s = AgySession(capture=args.capture, workdir=args.workdir, echo=args.echo)
    flag = "--print" if args.mode == "print" else "--prompt-interactive"
    print(f"[agy_session] starting: agy {flag} {args.prompt!r} (full union)", file=sys.stderr)
    s.start([flag, args.prompt])

    print("\n===== turn 1 (initial prompt) =====")
    if args.mode == "interactive" and not args.no_submit:
        settle = s.read_until_idle(idle=2.5, timeout=25)   # let TUI draw + answer queries
        s.send(b"\r")                                       # submit the prefilled prompt
        print(settle + s.read_until_idle(idle=args.idle, timeout=args.timeout))
    else:
        print(s.read_until_idle(idle=args.idle, timeout=args.timeout))
    for i, follow in enumerate(args.send, 2):
        print(f"\n===== turn {i}: sending {follow!r} =====")
        s.send_line(follow)
        print(s.read_until_idle(idle=args.idle, timeout=args.timeout))
    s.close()
    print("\n[agy_session] capture:", _summarize_capture(os.path.abspath(args.capture)), file=sys.stderr)


if __name__ == "__main__":
    main()
