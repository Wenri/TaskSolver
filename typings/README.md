These stubs describe the third-party interfaces TaskSolver uses where the dependency
does not supply typing: pyelftools, Capstone, pwntools and Brotli. BasedPyright loads
this directory by default.

Keep signatures aligned with the versions in `pyproject.toml`. Extend the relevant
stub when using another API; these files cover the consumed interfaces rather than
every API in each library. They are development files and do not ship in the wheel.
