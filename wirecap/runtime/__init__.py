"""wirecap.runtime — parent-side driver layer shared by every wrapper.

The generic machinery for launching a linked CLI and collecting its decoded turns:
the PTY/pipe launch + spawn-process handle, terminal auto-answer glue, the client drain
loops, git-workspace scoping, and managed terminal/web views. Unlike ``wirecap.decode``
this runs only in the parent, so non-stdlib dependencies are allowed. The embedded
interpreter's ``wirecap.decode`` modules must never import this package.
"""
