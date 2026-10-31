"""Offline procdef parsing checks: run from antigravity/ with
``pixi run python -m unittest symbols.test_procdef``.
"""
from pathlib import Path
import unittest

from .agyelf import active_rows, load_procdef, parse_procdef
from .gen_sources import BOOKS_OUT, render_hooks


class ProcdefTests(unittest.TestCase):
    def test_wrappers_and_symbol_literals(self):
        rows = parse_procdef('''
#define CAC "example/pkg.(*Client)."
{ "resp", WIRE_ASYNC, 0, AGY_FULLGO(CAC "Read(func())") },
{ "smoke", WIRE_SYNC, 0, AGY_ASMGO("os.Getenv") },
{ "special", WIRE_ASYNC, -1, AGY_OFF("crypto/tls.(*Conn).Read") },
{ "buffer", WIRE_ASYNC, 256, AGY_OFF("example.Buffer") },
''')
        self.assertEqual(rows, [
            {"id": "resp", "symbol": "example/pkg.(*Client).Read(func())",
             "mode": "async", "mech": "fullcgo", "retcap": 0},
            {"id": "smoke", "symbol": "os.Getenv",
             "mode": "sync", "mech": "asmcgo", "retcap": 0},
            {"id": "special", "symbol": "crypto/tls.(*Conn).Read",
             "mode": "async", "mech": "off", "retcap": -1},
            {"id": "buffer", "symbol": "example.Buffer",
             "mode": "async", "mech": "off", "retcap": 256},
        ])
        self.assertEqual([row["id"] for row in active_rows(rows)], ["resp", "smoke"])

    def test_checked_in_python_mirror(self):
        self.assertEqual(render_hooks(load_procdef()), Path(BOOKS_OUT).read_text())


if __name__ == "__main__":
    unittest.main()
