"""wirecap.decode — stdlib-pure capture/decode primitives shared by every wrapper.

IMPORT PURITY (load-bearing): this subpackage is imported by the embedded CPython worker
running inside the instrumented CLI (the module named by ``WIRE_MODULE``). It resolves from
the interpreter's own env site-packages (``site`` runs; ``PYTHONHOME`` selects the env), but
must still import with the standard library ALONE — third-party deps (brotli, hpack) are
imported lazily at the call site, and this layer must never import ``wirecap.runtime`` or
``tasksolver`` (both drag in heavy/parent-only deps). Two ``python3 -S`` tests enforce that
stdlib purity (they inject the path manually — they assert import-purity, not the runtime path).
"""
