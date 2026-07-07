"""wirecap.runtime — parent-side driver layer shared by every wrapper.

The generic machinery for launching an instrumented CLI and collecting its decoded turns:
the PTY/pipe launch + spawn-process handle, terminal auto-answer glue, the client drain
loops, and git-workspace scoping. Unlike ``wirecap.decode`` this runs only in the parent
(never the embedded interpreter), so non-stdlib deps are fine — but it must never be
imported BY ``wirecap.decode`` (keeps the embedded-import layer pure).
"""
