"""The one-pixmap-per-worker rule, made mechanical.

Risk R8 and the roadmap's week-1 gate say the same thing: *no more than one
page pixmap is live per worker*. That is easy to state, easy to believe, and
easy to break six months later with an innocent ``tiles = [render(t) for t in
...]``. So it is not a comment here — it is a counted, enforced invariant.

Every render in stage A goes through ``page_pixels``. Entering the context
increments a per-thread counter; leaving it drops the reference and
decrements. Entering a second scope while one is already open raises
``PixmapBudgetExceeded`` immediately, at the line that would have broken the
rule, rather than showing up as a memory number nobody can attribute.

What this does and does not prove
---------------------------------
* It **does** prove that stage A never holds two page/tile rasters at once:
  the guard is the only door, and ``tests/test_ingest_memory.py`` greps stage
  A for direct ``render_page`` calls to keep it that way.
* It **does not** measure bytes. It counts live raster objects. Actual memory
  is reported separately as ``PageTaskState.peak_rss_mb`` (see
  ``conduit.ingest.metrics``), measured rather than asserted.

The counter is per thread because the production profile runs one arq worker
per process/thread and the budget is per worker; a shared global would make
two legal concurrent workers look like a violation.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from conduit.errors import ConduitError
from conduit.geometry import BBox
from conduit.pdf.backend import PdfBackend, PixelEncoding, RenderedPage

__all__ = [
    "MAX_LIVE_PIXMAPS",
    "PixmapBudgetExceeded",
    "live_pixmaps",
    "page_pixels",
    "peak_live_pixmaps",
    "reset_pixmap_counters",
    "total_pixmaps_rendered",
]

#: One. Changing this constant is a design decision, not a tuning knob.
MAX_LIVE_PIXMAPS = 1


class PixmapBudgetExceeded(ConduitError):
    """A second page raster was requested while one was still live."""


class _Counters(threading.local):
    live: int = 0
    peak: int = 0
    total: int = 0


_counters = _Counters()


def live_pixmaps() -> int:
    return _counters.live


def peak_live_pixmaps() -> int:
    """High-water mark of concurrently live rasters on this thread."""
    return _counters.peak


def total_pixmaps_rendered() -> int:
    return _counters.total


def reset_pixmap_counters() -> None:
    _counters.live = 0
    _counters.peak = 0
    _counters.total = 0


@contextmanager
def page_pixels(
    backend: PdfBackend,
    page: int,
    *,
    dpi: int,
    clip: BBox | None = None,
    encoding: PixelEncoding = "raw",
) -> Iterator[RenderedPage]:
    """Render a page (or a ``clip`` of it) and guarantee it is released.

    Raises ``PixmapBudgetExceeded`` if another render scope is already open on
    this thread. Backend page-level errors (``CorruptPageError``,
    ``PageRenderError``) propagate unchanged — the caller records the page as
    failed and the run continues.
    """
    if _counters.live >= MAX_LIVE_PIXMAPS:
        raise PixmapBudgetExceeded(
            f"{_counters.live} page raster(s) already live; the budget is "
            f"{MAX_LIVE_PIXMAPS} per worker. Consume one render before starting "
            "the next (risk R8)."
        )
    _counters.live += 1
    _counters.peak = max(_counters.peak, _counters.live)
    _counters.total += 1
    rendered: RenderedPage | None = None
    try:
        rendered = backend.render_page(page, dpi=dpi, clip=clip, encoding=encoding)
        yield rendered
    finally:
        del rendered
        _counters.live -= 1
