#!/usr/bin/env python3
"""Generate pyagy/agy_process/hooks.py from the C hook table in src/procdef.h.

`pyagy.HOOKS` (+ by_mech/by_kind/enabled_hooks/sync_capable/DERIVED_KINDS) is a documented public
introspection surface, and it used to be maintained BY HAND against procdef.h — which drifted: it
sat 3 rows behind (missing EXIT, RESP_CHUNK and USAGE_DELTA, i.e. the clean end-of-capture marker
and the two hooks carrying the model response) and still keyed on a `leave` boolean after the C
struct had merged that column into the signed `retcap`.

Generating it removes that failure mode. Run after editing procdef.h:

    pixi run shim-hooks        # or: python3 antigravity/symbols/gen_hooks_py.py

The output IS checked in (unlike symbols_gen.h): it must ship in the wheel, and it has to stay
stdlib-pure because agy_process imports it inside the CLI's embedded interpreter.

usage: gen_hooks_py.py [<procdef.h> <hooks.py>]
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(_HERE, "..", "src", "procdef.h")
DEFAULT_OUT = os.path.join(_HERE, "..", "pyagy", "agy_process", "hooks.py")

# A row: { "ID", <symbol>, WIRE_x, "kind", AGY_x, retcap },
# <symbol> is either a quoted literal or a MACRO "suffix" concatenation (e.g. CAC "StreamGenerate").
_ROW = re.compile(
    r'\{\s*"(?P<id>[A-Z0-9_]+)"\s*,\s*'
    r'(?P<sym>(?:[A-Z_][A-Z0-9_]*\s*)?"(?:[^"\\]|\\.)*")\s*,\s*'
    r'(?P<mode>WIRE_\w+)\s*,\s*'
    r'"(?P<kind>[^"]*)"\s*,\s*'
    r'(?P<mech>AGY_\w+)\s*,\s*'
    r'(?P<retcap>-?\d+)\s*\}'
)
_MACRO_DEF = re.compile(r'^\s*#define\s+([A-Z_][A-Z0-9_]*)\s+("(?:[^"\\]|\\.)*")\s*$', re.M)


def _unquote(lit):
    return re.sub(r'\\(.)', r'\1', lit[1:-1])


def parse(src_text):
    """Every hook row from procdef.h, in table order."""
    macros = {name: _unquote(val) for name, val in _MACRO_DEF.findall(src_text)}
    rows = []
    for m in _ROW.finditer(src_text):
        sym = m.group("sym").strip()
        prefix = ""
        mac = re.match(r'^([A-Z_][A-Z0-9_]*)\s*(".*")$', sym, re.S)
        if mac:
            prefix = macros.get(mac.group(1), "")
            sym = mac.group(2)
        rows.append({
            "id": m.group("id"),
            "symbol": prefix + _unquote(sym),
            "mode": "sync" if m.group("mode") == "WIRE_SYNC" else "async",
            "kind": m.group("kind"),
            "mech": {"AGY_OFF": "off", "AGY_FULLCGO": "fullcgo",
                     "AGY_ASMCGO": "asmcgo"}.get(m.group("mech"), m.group("mech").lower()),
            "retcap": int(m.group("retcap")),
        })
    return rows


HEADER = '''"""Machine-readable mirror of the C hook table (src/procdef.h).

GENERATED — do not edit by hand. Regenerate with::

    pixi run shim-hooks

`pyagy.HOOKS` is a public introspection surface, so this file is checked in and ships in the
wheel. Stdlib-pure: agy_process imports it inside the instrumented CLI's embedded interpreter.

Each row mirrors one C row:
  id      short tag, the `hk("ID")` key at the C call sites
  symbol  the Go symbol the hook patches
  mode    "async" (log) | "sync" (block for a modify verdict)
  kind    the tag passed to dispatch(kind, stream_id, data)
  mech    "off" (not installed) | "fullcgo" | "asmcgo" — the cgocall trampoline flavour
  retcap  return-capture policy: 0 none, <0 special-cased, >0 min bytes. Every retcap != 0 hook
          is mech="off": the gum return-hook path it needed was retired and deleted, so this is
          the recorded register contract, not live behaviour.
"""

HOOKS = [
'''

FOOTER = ''']

#: kinds the Python layer DERIVES rather than receiving straight from a hook.
DERIVED_KINDS = ("genai_turn", "h2msg", "conversation_id", "callstack", "app_response")


def by_mech(mech):
    """Rows whose install mechanism is `mech` ("off" / "fullcgo" / "asmcgo")."""
    return [h for h in HOOKS if h["mech"] == mech]


def by_kind(kind):
    """Rows emitting `kind` (several hooks can share one kind)."""
    return [h for h in HOOKS if h["kind"] == kind]


def enabled_hooks():
    """Rows actually installed at runtime (everything not mech="off")."""
    return [h for h in HOOKS if h["mech"] != "off"]


def sync_capable():
    """Rows whose dispatch can return replacement bytes (mode="sync")."""
    return [h for h in HOOKS if h["mode"] == "sync"]
'''


def render(rows):
    out = [HEADER]
    for r in rows:
        out.append(
            '    {{"id": {id!r}, "symbol": {symbol!r},\n'
            '     "mode": {mode!r}, "kind": {kind!r}, "mech": {mech!r}, "retcap": {retcap!r}}},\n'
            .format(**r))
    out.append(FOOTER)
    return "".join(out)


def main(argv):
    src = argv[1] if len(argv) > 2 else DEFAULT_SRC
    out = argv[2] if len(argv) > 2 else DEFAULT_OUT
    rows = parse(open(src).read())
    if not rows:
        sys.stderr.write(f"[gen-hooks] no hook rows parsed from {src}\n")
        return 1
    open(out, "w").write(render(rows))
    n_off = sum(1 for r in rows if r["mech"] == "off")
    sys.stderr.write(f"[gen-hooks] wrote {out} ({len(rows)} hooks, {len(rows)-n_off} installed)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
