"""Resolve a native artifact that ships WITH its Python package — never an external path.

Both providers vendor large native artifacts (agy + the LD_PRELOAD shim; the from-source codex) and
resolve them the same three ways, so the lookup lives here once:

  1. in-package — a self-contained wheel bundles the artifact under the package itself
     (``pyagy/vendor/agy``, ``pycodex/vendor/codex``);
  2. the sibling checkout path — a source/editable install keeps it in the sibling build tree
     (``antigravity/vendor/agy``, ``codex/vendor/codex-rs/target/release/codex``);
  3. an installed wheel elsewhere on ``sys.path`` — a development checkout does not necessarily
     contain the large native artifacts, so reuse them from the installed wheel while keeping the
     Python adapter itself on the editable source tree.

There is deliberately NO env override: the artifacts are build-id/ABI coupled to the package (the
shim's symbol offsets are resolved against exactly the vendored agy), so pointing this at an
arbitrary binary produces a silently non-hooking run.
"""
import os
import sys


def vendored(pkg_dir, pkg_name, in_pkg_rel, sibling_rel):
    """Resolve ``in_pkg_rel`` for the package rooted at ``pkg_dir`` (import name ``pkg_name``).

    Returns the first of the three candidates that exists, else the sibling path — so the caller
    gets a real path to report in the "missing artifact" error rather than None.

    A found path is returned symlink-resolved: when ``pkg_dir`` is reached through a symlink,
    ``<pkg_dir>/../<sibling>`` exists only as the kernel walks it (``..`` of the link TARGET),
    and callers that ``os.path.abspath`` the result would collapse the ``..`` lexically onto the
    link's own parent and miss the artifact."""
    in_pkg = os.path.join(pkg_dir, in_pkg_rel)
    if os.path.exists(in_pkg):
        return os.path.realpath(in_pkg)
    sibling = os.path.join(pkg_dir, sibling_rel)
    if os.path.exists(sibling):
        return os.path.realpath(sibling)
    for entry in sys.path:
        candidate = os.path.join(entry, pkg_name, in_pkg_rel)
        if os.path.exists(candidate):
            return os.path.realpath(candidate)
    return sibling
