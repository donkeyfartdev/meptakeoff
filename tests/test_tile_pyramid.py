"""Tile pyramid geometry: level count, level sizes, tile grid, level sources.

``plan_pyramid`` is pure arithmetic, so most of this needs no PDF at all —
which is the point: the geometry that a review UI and a detector both depend on
is checkable without rendering anything.
"""

from __future__ import annotations

import io
import json
import math

import pytest

from conduit.ingest.config import MIN_BACKEND_DPI, StageAConfig
from conduit.ingest.tiles import build_pyramid, plan_pyramid, render_full_page


@pytest.mark.parametrize(
    ("w", "h", "expected_max_zoom"),
    [
        (256, 256, 0),      # already one tile
        (257, 256, 1),
        (1024, 512, 2),
        (7200, 4800, 5),    # ARCH D at 200 dpi -> 7200/32 = 225 <= 256
        (9600, 7200, 6),    # ARCH E at 200 dpi
    ],
)
def test_level_count(w: int, h: int, expected_max_zoom: int) -> None:
    plan = plan_pyramid(w, h, tile_size=256, base_dpi=200)
    assert plan.max_zoom == expected_max_zoom
    assert len(plan.levels) == expected_max_zoom + 1
    top = plan.levels[0]
    assert top.cols == top.rows == 1, "z=0 fits in a single tile, by definition"
    base = plan.levels[-1]
    assert (base.width_px, base.height_px) == (w, h)
    assert base.denominator == 1


def test_level_geometry_halves_and_tiles_cover_the_level() -> None:
    plan = plan_pyramid(9600, 7200, tile_size=256, base_dpi=200)
    for lvl in plan.levels:
        assert lvl.width_px == max(1, math.ceil(9600 / lvl.denominator))
        assert lvl.height_px == max(1, math.ceil(7200 / lvl.denominator))
        assert lvl.cols == math.ceil(lvl.width_px / 256)
        assert lvl.rows == math.ceil(lvl.height_px / 256)
        # The grid covers the level exactly once, with the last row/column
        # possibly short — no gaps, no overlap.
        assert (lvl.cols - 1) * 256 < lvl.width_px <= lvl.cols * 256
        assert (lvl.rows - 1) * 256 < lvl.height_px <= lvl.rows * 256


def test_only_legal_dpis_are_requested_from_the_backend() -> None:
    """Levels coarser than the backend's minimum DPI must be downsampled.

    ``PageGeometry`` refuses < 36 dpi (mirroring ``ck_sheet_dpi``), so asking
    for 12.5 dpi would raise instead of producing a tile.
    """
    plan = plan_pyramid(9600, 7200, tile_size=256, base_dpi=200)
    for lvl in plan.levels:
        assert lvl.render_dpi >= MIN_BACKEND_DPI
        if lvl.source == "direct":
            assert lvl.render_dpi * lvl.denominator == 200
        else:
            assert lvl.render_dpi == MIN_BACKEND_DPI
    assert [lvl.source for lvl in plan.levels[-3:]] == ["direct"] * 3
    assert plan.levels[0].source == "downsampled"


def test_degenerate_page_is_rejected() -> None:
    with pytest.raises(ValueError):
        plan_pyramid(0, 100)


# --- against a real page --------------------------------------------------


def test_built_pyramid_matches_its_plan(backend, store) -> None:
    from PIL import Image

    cfg = StageAConfig(render_dpi=72, tile_size=256)
    geom = backend.page_geometry(1, dpi=cfg.render_dpi)
    result = build_pyramid(backend, geom, store, cfg)

    plan = plan_pyramid(
        geom.width_px, geom.height_px, tile_size=cfg.tile_size, base_dpi=cfg.render_dpi
    )
    assert result.max_zoom == plan.max_zoom
    assert result.tile_count == plan.tile_count
    assert result.stored_object_count <= result.tile_count + 1, "blank tiles dedupe"

    manifest = json.loads(store.get_bytes(result.manifest_key))
    assert manifest["rotation_deg"] == geom.rotation_deg
    for lvl, planned in zip(manifest["levels"], plan.levels, strict=True):
        assert lvl["z"] == planned.z
        assert (lvl["width_px"], lvl["height_px"]) == (planned.width_px, planned.height_px)
        assert len(lvl["tiles"]) == planned.cols * planned.rows

    # Decode a real tile and check its pixel size matches the manifest's grid.
    base = manifest["levels"][-1]
    img = Image.open(io.BytesIO(store.get_bytes(base["tiles"]["0/0"])))
    assert img.format == "WEBP"
    assert img.size == (
        min(cfg.tile_size, base["width_px"]),
        min(cfg.tile_size, base["height_px"]),
    )
    # ...and the last column, which is the one that gets truncated.
    last_x = base["cols"] - 1
    edge = Image.open(io.BytesIO(store.get_bytes(base["tiles"][f"{last_x}/0"])))
    assert edge.width == base["width_px"] - last_x * cfg.tile_size


def test_thumbnail_is_bounded(backend, store) -> None:
    from PIL import Image

    cfg = StageAConfig(render_dpi=72, thumbnail_max_px=300)
    geom = backend.page_geometry(3, dpi=cfg.render_dpi)
    result = build_pyramid(backend, geom, store, cfg)
    img = Image.open(io.BytesIO(store.get_bytes(result.thumbnail_key)))
    assert max(img.size) == cfg.thumbnail_max_px
    assert img.size[0] / img.size[1] == pytest.approx(
        geom.width_px / geom.height_px, rel=0.02
    )


def test_full_page_raster_is_skipped_above_the_pixel_budget(backend, store) -> None:
    """The budget exists so one page never costs 200 MB of samples."""
    geom = backend.page_geometry(1, dpi=200)
    tight = StageAConfig(render_dpi=200, full_page_raster_max_pixels=1_000)
    assert render_full_page(backend, geom, store, tight) is None

    small = backend.page_geometry(1, dpi=36)
    cfg = StageAConfig(render_dpi=36, full_page_raster_max_pixels=40_000_000)
    out = render_full_page(backend, small, store, cfg)
    assert out is not None
    key, size = out
    assert store.exists(key) and size > 0
