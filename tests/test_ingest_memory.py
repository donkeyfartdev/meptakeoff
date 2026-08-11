"""The one-page-raster-per-worker rule (risk R8), asserted rather than asserted-to.

The roadmap's week-1 memory gate has two halves. The measured half — peak RSS
— is recorded per page in ``PageTaskState`` and summarised in
``bench/RESULTS.md``. The structural half is here: *no more than one page
pixmap is live per worker*, which is a property of the code and can be tested
deterministically instead of inferred from a memory graph.

Three things are checked:

1. the budget is enforced (a second concurrent render raises);
2. a full stage A run over the corpus never exceeds it, even though it renders
   hundreds of tiles;
3. stage A cannot quietly route around the guard — no module in
   ``conduit/ingest`` calls ``render_page`` directly except ``render.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conduit.ingest.render import (
    MAX_LIVE_PIXMAPS,
    PixmapBudgetExceeded,
    live_pixmaps,
    page_pixels,
    peak_live_pixmaps,
    total_pixmaps_rendered,
)

INGEST_DIR = Path(__file__).resolve().parents[1] / "conduit" / "ingest"


def test_the_budget_is_one() -> None:
    assert MAX_LIVE_PIXMAPS == 1


def test_a_second_live_render_is_refused(backend) -> None:
    with page_pixels(backend, 1, dpi=36):
        assert live_pixmaps() == 1
        with pytest.raises(PixmapBudgetExceeded):
            with page_pixels(backend, 2, dpi=36):
                pass
    assert live_pixmaps() == 0


def test_the_counter_returns_to_zero_when_a_render_fails(backend, manifest) -> None:
    """A corrupt page must not leak a slot, or the next page is refused."""
    from conduit.errors import CorruptPageError

    with pytest.raises(CorruptPageError):
        with page_pixels(backend, manifest.corrupt_pages[0], dpi=36):
            pass
    assert live_pixmaps() == 0
    with page_pixels(backend, 1, dpi=36):
        assert live_pixmaps() == 1


def test_a_whole_stage_a_run_never_holds_two_rasters(ingested) -> None:
    """The counters below are from the shared session-scoped ingest run."""
    assert ingested.peak_live_pixmaps == 1
    # Fewer renders than tiles, by design: the coarse levels and the
    # thumbnail are all cut from ONE 36 dpi render (conduit/ingest/tiles.py).
    assert 0 < total_pixmaps_rendered() < ingested.result.tile_count
    assert peak_live_pixmaps() == 1


def test_only_the_guard_calls_render_page() -> None:
    """Stage A must not route around ``page_pixels``.

    Same shape as ``tests/test_no_direct_paths.py``: the rule is enforced by
    reading the tree, not by hoping.
    """
    offenders = []
    for py in sorted(INGEST_DIR.rglob("*.py")):
        if py.name == "render.py":
            continue
        src = re.sub(r'""".*?"""', "", py.read_text(), flags=re.S)
        for lineno, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\.render_page\s*\(", code):
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "stage A renders only through conduit.ingest.render.page_pixels:\n"
        + "\n".join(offenders)
    )


def test_rss_readings_do_not_grow_page_after_page(ingested) -> None:
    """A streaming stage stops growing; a leaking one does not.

    ``peak_rss_mb`` is a process high-water mark (see ``conduit.ingest.metrics``
    for exactly what that does and does not mean), so it can only ever rise.
    What a leak would look like is a rise that keeps happening on *every* page.
    The corpus is small, so this is a smoke check with a deliberately generous
    bound rather than a precise gate; the real number is measured at 200 dpi
    into ``bench/RESULTS.md``.
    """
    deltas = [
        stage.rss_delta_mb
        for page in ingested.result.pages
        for stage in page.stages
    ]
    assert deltas, "every page/stage records an RSS delta"
    later = deltas[len(deltas) // 2 :]
    assert sum(1 for d in later if d > 0) <= len(later) // 2, (
        f"RSS still climbing on most later pages: {later}"
    )
