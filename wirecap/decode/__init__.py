"""Capture/decode primitives that remain importable with only the standard library.

The linked CLI's embedded CPython worker imports this package through ``WIRE_MODULE``.
It resolves from the environment selected by ``PYTHONHOME``, but imports must also work
under ``python3 -S``. Brotli is loaded when used; the optional hpack import tolerates its
absence. Never import the parent-side ``wirecap.runtime`` or ``tasksolver`` packages here.
"""
