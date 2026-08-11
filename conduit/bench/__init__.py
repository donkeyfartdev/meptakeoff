"""Benchmark and corpus tooling.

``make_corpus`` builds the synthetic plan set that Slice A is tested against.
It is SYNTHETIC: it exercises geometry, rotation, raster-vs-vector and failure
paths, and it must never be used to produce an accuracy number. See
``bench/CORPUS.md``.

``run_set.py`` (roadmap W1 task 7) is not built yet.
"""
