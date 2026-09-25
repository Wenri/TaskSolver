"""Give the SDK's stdio subprocess its own group before executing Codex."""

import os
import sys


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m pycodex.sdk_process CODEX [ARGS...]")
    os.setsid()
    os.execv(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
