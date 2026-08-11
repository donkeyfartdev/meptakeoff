"""Coordinate-space tests.

The roadmap's week-1 check is: "a raster_px box -> pdf_points -> raster_px
returns within 0.01 px for rotation_deg in {0, 90, 180, 270}". That is
``test_corpus_roundtrip_within_hundredth_px``, run against the real backend
reading the real (synthetic) corpus, so it exercises the geometry the backend
actually reports rather than a hand-written fixture.
"""

from __future__ import annotations

import pytest

from conduit.errors import PageGeometryError, UnsupportedRotationError
from conduit.geometry import BBox, PageGeometry, Point, normalize_rotation

ARCH_D = (36 * 72.0, 24 * 72.0)
TOL_PX = 0.01


def geom(rotation: int, dpi: int = 200, origin: tuple[float, float] = (0.0, 0.0)) -> PageGeometry:
    x0, y0 = origin
    return PageGeometry(
        page_number=1,
        media_box_x0=x0,
        media_box_y0=y0,
        media_box_x1=x0 + ARCH_D[0],
        media_box_y1=y0 + ARCH_D[1],
        rotation_deg=rotation,
        render_dpi=dpi,
    )


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("origin", [(0.0, 0.0), (12.0, -7.5)])
def test_point_roundtrip(rotation: int, origin: tuple[float, float]) -> None:
    g = geom(rotation, origin=origin)
    for px in [Point(0, 0), Point(1.5, 2.5), Point(g.width_px - 1, g.height_px - 1)]:
        back = g.pdf_to_raster(g.raster_to_pdf(px))
        assert abs(back.x - px.x) < TOL_PX
        assert abs(back.y - px.y) < TOL_PX


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_bbox_roundtrip(rotation: int) -> None:
    g = geom(rotation)
    box = BBox(101.25, 88.5, 640.75, 1200.125)
    back = g.pdf_bbox_to_raster(g.raster_bbox_to_pdf(box))
    for a, b in zip(box.as_tuple(), back.as_tuple(), strict=True):
        assert abs(a - b) < TOL_PX


@pytest.mark.parametrize(
    ("rotation", "expected_corner"),
    [
        # The unrotated page's top-left corner, after /Rotate, in raster px.
        (0, "top-left"),
        (90, "top-right"),
        (180, "bottom-right"),
        (270, "bottom-left"),
    ],
)
def test_rotation_corner_mapping(rotation: int, expected_corner: str) -> None:
    """Pin the direction of rotation. /Rotate 90 displays the page clockwise."""
    g = geom(rotation)
    top_left_pdf = Point(g.media_box_x0, g.media_box_y1)
    p = g.pdf_to_raster(top_left_pdf)
    corners = {
        "top-left": (0.0, 0.0),
        "top-right": (g.width_px, 0.0),
        "bottom-right": (g.width_px, g.height_px),
        "bottom-left": (0.0, g.height_px),
    }
    ex, ey = corners[expected_corner]
    assert abs(p.x - ex) < TOL_PX and abs(p.y - ey) < TOL_PX


@pytest.mark.parametrize(("rotation", "swapped"), [(0, False), (90, True), (180, False), (270, True)])
def test_rotation_swaps_raster_dimensions(rotation: int, swapped: bool) -> None:
    g = geom(rotation)
    portrait_is_landscape = g.width_px > g.height_px
    assert portrait_is_landscape is not swapped


def test_lengths_are_rotation_invariant() -> None:
    for rot in (0, 90, 180, 270):
        g = geom(rot)
        assert g.px_to_pt(200.0) == pytest.approx(72.0)
        assert g.pt_to_px(72.0) == pytest.approx(200.0)


def test_normalize_rotation() -> None:
    assert normalize_rotation(-90) == 270
    assert normalize_rotation(450) == 90
    with pytest.raises(UnsupportedRotationError):
        normalize_rotation(45)


def test_degenerate_mediabox_is_typed_error() -> None:
    with pytest.raises(PageGeometryError):
        PageGeometry(
            page_number=1,
            media_box_x0=0,
            media_box_y0=0,
            media_box_x1=0,
            media_box_y1=100,
            rotation_deg=0,
        )


def test_dpi_bounds_mirror_the_orm_check() -> None:
    with pytest.raises(PageGeometryError):
        geom(0, dpi=12)
    with pytest.raises(PageGeometryError):
        geom(0, dpi=2400)


# --- the roadmap's week-1 check, against the real backend -----------------


def test_corpus_roundtrip_within_hundredth_px(backend, manifest) -> None:
    """raster_px -> pdf_points -> raster_px, every page of the corpus.

    The corpus deliberately contains /Rotate 0, 90, 180 and 270 pages in two
    MediaBox sizes; the assertion below also proves all four rotations were
    actually present, so this cannot pass by testing 24 unrotated pages.
    """
    seen_rotations = set()
    for page in range(1, manifest.page_count + 1):
        g = backend.page_geometry(page, dpi=200)
        seen_rotations.add(g.rotation_deg)
        box = BBox(10.0, 20.0, g.width_px - 30.0, g.height_px - 40.0)
        back = g.pdf_bbox_to_raster(g.raster_bbox_to_pdf(box))
        for a, b in zip(box.as_tuple(), back.as_tuple(), strict=True):
            assert abs(a - b) < TOL_PX, f"page {page} rot {g.rotation_deg}: {a} vs {b}"
    assert seen_rotations == {0, 90, 180, 270}


def test_corpus_page_sizes_are_arch_d_and_arch_e(backend, manifest) -> None:
    sizes = set()
    for page in range(1, manifest.page_count + 1):
        g = backend.page_geometry(page)
        sizes.add((round(g.width_pt / 72), round(g.height_pt / 72)))
    assert sizes == {(36, 24), (48, 36)}
