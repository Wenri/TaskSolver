"""Terminal glue shared by every agy PTY driver.

`agy` is a TUI: it emits ANSI escapes we must strip to read its output, and it
sends terminal-capability queries at startup that it *blocks on* until answered.
Both the one-shot (`session.run_print`) and multi-turn (`session.InteractiveSession`,
`test_scripts/agy_session.py`) drivers need the exact same two things, so they live
here once instead of being copied three times.
"""
import re

# OSC/DCS/CSI escapes + stray control chars (keep \t \n \r). This is the union of
# what agy emits; anything left after .sub() is human-readable transcript text.
_ANSI = re.compile(
    r"""\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)   # OSC ... BEL/ST
      | \x1b[P^_][^\x1b]*\x1b\\             # DCS/PM/APC ... ST
      | \x1b\[[0-9;?]*[ -/]*[@-~]           # CSI
      | \x1b[@-Z\\-_]                       # 2-byte escapes
      | [\x00-\x08\x0b\x0c\x0e-\x1f]        # stray control chars (keep \t \n \r)
    """,
    re.VERBOSE,
)


def strip_ansi(b) -> str:
    """Decode (if bytes) and strip ANSI/control sequences → plain transcript text."""
    if isinstance(b, (bytes, bytearray)):
        b = bytes(b).decode("utf-8", "replace")
    return _ANSI.sub("", b)


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
