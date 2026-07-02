"""Importable tool handler for test_config.py's module:func handler test.
(A handler must live in an importable module — not the __main__ entry script — so
the MCP server subprocess can import it. This module exists to be that module.)"""


def echo_upper(args):
    return "HANDLED:" + str(args.get("q", "")).upper()
