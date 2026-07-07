"""wirecap.decode — stdlib-pure capture/decode primitives shared by every wrapper.

IMPORT PURITY (load-bearing): this subpackage is imported by the embedded CPython worker
running inside the instrumented CLI (the module named by ``WIRE_MODULE``), under a bare
libpython with only ``WIRE_PYTHONPATH`` on ``sys.path``. Everything here must import with
the standard library alone — third-party deps (brotli, hpack) are imported lazily at the
call site, and this layer must never import ``wirecap.runtime`` or ``tasksolver``. Two
``python3 -S`` tests enforce it.
"""
