#!/usr/bin/env python3
"""Drive an interactive `agy` session under a controlled PTY + the antigravity network
hooks. This is the "session" layer: agy is a TUI that wants a real terminal, so we
give it a pty we own — letting us read its rendered output and inject further input,
while the LD_PRELOAD hooks capture the model traffic (clean, ANSI-free) in parallel.

Two channels of "output":
  * PTY transcript  — what agy renders (ANSI-stripped here). Good for UI state.
  * network capture — the actual Gemini request/response (via crypto/tls hooks),
                      written to the AGY_PROC_CAPTURE JSONL. Cleaner for content.

Usage:
    python3 agy_session.py --mode interactive --prompt "what is 2+2" \
        --send "and times 10?" --idle 4 --timeout 120 --stage 3
    python3 agy_session.py --mode print --prompt "what is 2+2"   # one-shot

As a library:
    s = AgySession(stage=3, capture="cap.jsonl", workdir="/tmp/ws")
    s.start(["--prompt-interactive", "what is 2+2"])
    print(s.read_until_idle(idle=4, timeout=120))     # assistant's first answer
    s.send_line("and multiply by 10?")
    print(s.read_until_idle(idle=4, timeout=120))
    s.close()
"""
import argparse
import os
import pty
import re
import select
import signal
import sys
import time

ANSI = re.compile(
    r"""\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)   # OSC ... BEL/ST
      | \x1b[P^_][^\x1b]*\x1b\\             # DCS/PM/APC ... ST
      | \x1b\[[0-9;?]*[ -/]*[@-~]           # CSI
      | \x1b[@-Z\\-_]                       # 2-byte escapes
      | [\x00-\x08\x0b\x0c\x0e-\x1f]        # stray control chars (keep \t \n \r)
    """,
    re.VERBOSE,
)


def strip_ansi(b: bytes) -> str:
    return ANSI.sub("", b.decode("utf-8", "replace"))


class AgySession:
    def __init__(self, agy=None, stage=3, capture="agy-capture.jsonl",
                 log=None, workdir=None, extra_env=None, echo=False):
        here = os.path.dirname(os.path.abspath(__file__))
        self.root = os.path.dirname(here)                       # antigravity/
        self.agy = agy or os.path.expanduser("~/.local/bin/agy")
        self.stage = stage
        self.capture = os.path.abspath(capture)
        self.log = os.path.abspath(log) if log else None
        self.workdir = workdir or os.getcwd()
        self.echo = echo
        self.extra_env = extra_env or {}
        self.pid = None
        self.fd = None
        self.raw = bytearray()           # all bytes read from the pty
        self._qpos = 0                   # scan position for terminal-query auto-replies

    # agy's TUI queries the terminal and blocks until answered. Emulate a real
    # terminal by replying to the common queries so the UI proceeds.
    _QUERIES = [
        (re.compile(rb"\x1b\[\?(\d+)\$p"), lambda m: b"\x1b[?" + m.group(1) + b";0$y"),  # DECRQM
        (re.compile(rb"\x1b\[>0?q"),       lambda m: b"\x1bP>|antigravity 0.1\x1b\\"),        # XTVERSION
        (re.compile(rb"\x1b\[\?u"),        lambda m: b"\x1b[?0u"),                        # kitty kbd query
        (re.compile(rb"\x1b\[>0?c"),       lambda m: b"\x1b[>0;10;1c"),                   # secondary DA
        (re.compile(rb"\x1b\[0?c"),        lambda m: b"\x1b[?1;2c"),                      # primary DA
        (re.compile(rb"\x1b\[6n"),         lambda m: b"\x1b[50;200R"),                    # cursor pos
        (re.compile(rb"\x1b\[5n"),         lambda m: b"\x1b[0n"),                         # device status
    ]

    def _answer_queries(self):
        while True:
            best = None
            for rx, rep in self._QUERIES:
                m = rx.search(self.raw, self._qpos)
                if m and (best is None or m.start() < best[0].start()):
                    best = (m, rep)
            if not best:
                # keep a small tail in case a query is split across reads
                self._qpos = max(self._qpos, len(self.raw) - 8)
                return
            m, rep = best
            try:
                os.write(self.fd, rep(m))
            except OSError:
                return
            self._qpos = m.end()

    def _env(self):
        env = dict(os.environ)
        env.update({
            "AGY_PROC_ENABLE": "1",
            "AGY_PROC_STAGE": str(self.stage),
            "AGY_PROC_MODULE": "agy_process",
            "AGY_PROC_PYTHONPATH": os.path.join(self.root, "python"),
            "AGY_PROC_CAPTURE": self.capture,
            "PYTHONPATH": os.path.join(self.root, "python") + os.pathsep + env.get("PYTHONPATH", ""),
            "LD_PRELOAD": os.path.join(self.root, "vendor", "antigravity.so")
                          + (os.pathsep + env["LD_PRELOAD"] if env.get("LD_PRELOAD") else ""),
            "GODEBUG": ("netdns=cgo," + env.get("GODEBUG", "")).rstrip(","),
            "TERM": env.get("TERM", "xterm-256color"),
        })
        if self.log:
            env["AGY_PROC_LOG"] = self.log
        env.update(self.extra_env)
        return env

    def start(self, args):
        """Fork agy under a pty. `args` are appended after the agy binary."""
        argv = [self.agy] + list(args)
        env = self._env()
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(self.workdir)
                os.execve(self.agy, argv, env)
            except Exception as e:  # pragma: no cover
                os.write(2, f"exec failed: {e}\n".encode())
                os._exit(127)
        self.pid, self.fd = pid, fd
        try:
            import termios, struct, fcntl
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        except Exception:
            pass
        return self

    def _pump(self, timeout):
        """Read available bytes for up to `timeout` seconds; return what was read."""
        got = bytearray()
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], min(0.3, max(0.0, end - time.time())))
            if not r:
                break
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            got += chunk
            self.raw += chunk
            self._answer_queries()
            if self.echo:
                os.write(1, chunk)
        return got

    def read_until_idle(self, idle=3.0, timeout=120.0):
        """Read until no new output for `idle` s (agent done talking) or `timeout`."""
        start = time.time()
        last = time.time()
        buf = bytearray()
        while time.time() - start < timeout:
            chunk = self._pump(min(idle, 1.0))
            if chunk:
                buf += chunk
                last = time.time()
            elif time.time() - last >= idle:
                break
            if self.pid and self._exited():
                buf += self._pump(0.5)
                break
        return strip_ansi(bytes(buf))

    def send(self, data: bytes):
        os.write(self.fd, data)

    def send_line(self, text: str):
        """Type a line and press Enter (CR is what TUIs expect)."""
        self.send(text.encode() + b"\r")

    def _exited(self):
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return pid != 0
        except ChildProcessError:
            return True

    def close(self):
        if not self.pid:
            return
        for sig in (b"\x03", b"\x03"):   # Ctrl-C twice
            try:
                self.send(sig); time.sleep(0.3)
            except OSError:
                break
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def _summarize_capture(path):
    import collections, json
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
    ap.add_argument("--stage", type=int, default=1,
                    help="1=python+DNS. 3=tls_write+decrypt (request+response capture, works). "
                         "5=tls_read/RoundTrip (park-while-hooked → STALL agy).")
    ap.add_argument("--capture", default="agy-capture.jsonl")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--echo", action="store_true", help="mirror agy output to our stdout live")
    ap.add_argument("--no-submit", action="store_true",
                    help="don't auto-press Enter after startup (interactive prefills the prompt)")
    args = ap.parse_args()

    s = AgySession(stage=args.stage, capture=args.capture, workdir=args.workdir, echo=args.echo)
    flag = "--print" if args.mode == "print" else "--prompt-interactive"
    print(f"[agy_session] starting: agy {flag} {args.prompt!r} (stage {args.stage})", file=sys.stderr)
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
