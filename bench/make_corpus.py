"""Thin entry point so `python -m bench.make_corpus` works from the repo root.

The implementation lives in ``conduit/bench/make_corpus.py`` (importable as
``conduit.bench.make_corpus``, which is the module path the roadmap uses for
the other harnesses). This shim exists because generated corpora and their
manifests belong in ``bench/out/``, next to ``bench/CORPUS.md``, not inside the
installed package.
"""

from __future__ import annotations

import sys

from conduit.bench.make_corpus import main

if __name__ == "__main__":
    sys.exit(main())
