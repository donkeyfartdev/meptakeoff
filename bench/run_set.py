"""Thin entry point so `python -m bench.run_set` works from the repo root.

The implementation lives in ``conduit/bench/run_set.py`` (importable as
``conduit.bench.run_set``, the module path the roadmap uses). This shim exists
for the same reason as ``bench/make_corpus.py``: bench output belongs in
``bench/``, next to ``bench/RESULTS.md``, not inside the installed package.
"""

from __future__ import annotations

import sys

from conduit.bench.run_set import main

if __name__ == "__main__":
    sys.exit(main())
