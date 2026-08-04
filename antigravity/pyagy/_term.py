"""Terminal glue shared by every agy PTY driver.

`agy` is a TUI: it emits ANSI escapes we must strip to read its output, and it
sends terminal-capability queries at startup that it *blocks on* until answered.
Both the one-shot (`client.ask`) and multi-turn (`client.Session`,
`test_scripts/agy_session.py`) drivers need the exact same two things, so they live
here once instead of being copied.
"""
import re

# ANSI stripping is not agy-specific — it lives with the shared PTY driver. Re-exported here
# because `pyagy.strip_ansi` is a documented public name (and test_scripts imports it from
# `pyagy._term`); the agy-only pieces below (query/trust auto-reply, answer_text) stay put.
from wirecap.runtime.pty import strip_ansi  # noqa: F401


# Our own shim/log lines that leak onto agy's PTY (the shim logs to stderr, which the pty
# merges with stdout); an instrumented run's transcript carries them, so drop them to
# recover the clean answer text.
_LOG_MARKERS = ("[antigravity", "[wirecap", "[agy_process]", "gohook", "gomod")


def answer_text(transcript) -> str:
    """The clean answer from a (possibly instrumented) PTY transcript: drop our shim log
    lines and blank lines, keep the rest."""
    lines = [ln for ln in transcript.splitlines()
             if ln.strip() and not any(m in ln for m in _LOG_MARKERS)]
    return "\n".join(lines).strip()


# Terminal-capability queries agy sends and blocks on; reply like a real terminal.
# (DECRQM, XTVERSION, kitty-kbd, secondary/primary DA, cursor-pos, device-status.)
_QUERIES = [
    (re.compile(rb"\x1b\[\?(\d+)\$p"), lambda m: b"\x1b[?" + m.group(1) + b";0$y"),
    (re.compile(rb"\x1b\[>0?q"),       lambda m: b"\x1bP>|pyagy\x1b\\"),
    (re.compile(rb"\x1b\[\?u"),        lambda m: b"\x1b[?0u"),
    (re.compile(rb"\x1b\[>0?c"),       lambda m: b"\x1b[>0;10;1c"),
    (re.compile(rb"\x1b\[0?c"),        lambda m: b"\x1b[?1;2c"),
    (re.compile(rb"\x1b\[6n"),         lambda m: b"\x1b[50;200R"),
    (re.compile(rb"\x1b\[5n"),         lambda m: b"\x1b[0n"),
]


def answer_queries(raw, qpos, writer) -> int:
    """Scan ``raw[qpos:]`` for terminal-capability queries agy is blocking on and
    reply to each (earliest-match first) via ``writer(bytes)``. Returns the new scan
    position — pass it back on the next call. Keeps an 8-byte tail unscanned in case
    a query straddles two reads."""
    while True:
        best = None
        for rx, rep in _QUERIES:
            m = rx.search(raw, qpos)
            if m and (best is None or m.start() < best[0].start()):
                best = (m, rep)
        if not best:
            return max(qpos, len(raw) - 8)
        m, rep = best
        try:
            writer(rep(m))
        except OSError:
            return qpos
        qpos = m.end()


# The folder-trust menu agy shows at interactive startup on an untrusted workspace
# ("Antigravity CLI requires permission to read, edit, and execute files here." with
# "> Yes, I trust this folder" pre-selected). It BLOCKS the TUI until answered, which is
# the main cause of interactive hangs. `--print` never shows it.
_TRUST_MENU = re.compile(rb"trust this folder", re.IGNORECASE)


def answer_trust(raw, writer) -> bool:
    """If the folder-trust menu is present, press Enter once (its default selection is
    "Yes, I trust this folder") to unblock the TUI. Returns True once it has answered so
    the caller can stop trying (the menu redraws, so answer at most once)."""
    if not _TRUST_MENU.search(raw):
        return False
    try:
        writer(b"\r")
    except OSError:
        return False
    return True
