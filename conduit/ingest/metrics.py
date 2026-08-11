"""Timing and memory readings for ``PageTaskState``.

Both columns (``duration_ms``, ``peak_rss_mb``) already exist in the schema;
this module is what puts real numbers in them.

What ``peak_rss_mb`` actually means here — read this before quoting it
--------------------------------------------------------------------
``psutil`` is not installed on this machine and pipeline code may not open
``/proc`` (``tests/test_no_direct_paths.py``), so the reading comes from
``resource.getrusage(RUSAGE_SELF).ru_maxrss``: the **process-wide high-water
mark of resident set size**, in kilobytes on Linux.

Consequences, stated rather than smoothed over:

* It never decreases. The value stored on page 7 is "the largest this process
  had ever been when page 7 finished", not "what page 7 cost".
* Therefore the useful readings are the **maximum over a run** (a genuine peak
  for the worker, which is what the R8 gate is about) and the **increment
  between consecutive pages** (which is the honest signal for a leak: a
  streaming stage A should stop growing after the first few pages).
* ``rss_delta_mb`` on ``StageTiming`` records that increment so the leak
  question can be answered from the data instead of by assertion.

A per-page *cost* number would need either ``psutil`` or a sampling thread
reading ``/proc/self/statm``; neither is available under the current
constraints. This is written down rather than approximated.
"""

from __future__ import annotations

import resource
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

__all__ = ["StageTiming", "process_peak_rss_mb", "stage_timer"]


def process_peak_rss_mb() -> int:
    """Process high-water RSS in whole MB (see the module docstring)."""
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(kb // 1024)


@dataclass
class StageTiming:
    """Filled in by ``stage_timer``; copied straight onto ``PageTaskState``."""

    duration_ms: int = 0
    peak_rss_mb: int = 0
    rss_delta_mb: int = 0
    started_monotonic: float = field(default=0.0, repr=False)

    @property
    def seconds(self) -> float:
        return self.duration_ms / 1000.0


@contextmanager
def stage_timer() -> Iterator[StageTiming]:
    """Time a page/stage task and record the process RSS high-water mark.

    The timing is recorded even when the body raises, because a failed page's
    duration is exactly as interesting as a successful one's.
    """
    timing = StageTiming()
    rss_before = process_peak_rss_mb()
    timing.started_monotonic = time.perf_counter()
    try:
        yield timing
    finally:
        timing.duration_ms = int(round((time.perf_counter() - timing.started_monotonic) * 1000))
        timing.peak_rss_mb = process_peak_rss_mb()
        timing.rss_delta_mb = timing.peak_rss_mb - rss_before
