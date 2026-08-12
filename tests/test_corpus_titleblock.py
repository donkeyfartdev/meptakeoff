"""The corpus's title blocks must be readable, or stage B is untested.

Two defects this file pins, both found by reading the generated PDF back
rather than by reading the generator:

1. **Wrapped tokens.** ``insert_textbox(..., rotate=90)`` wraps mid-token when
   the rect is too narrow in the writing direction. Page 7 genuinely contained
   the spans ``M-1`` and ``02`` — never ``M-102``. A corpus like that cannot
   exercise rotated title-block reading at all; a classifier tested against it
   would be tested against input that guarantees it fails.
2. **Placement ignoring ``/Rotate``.** The block was drawn at the page-space
   bottom-right regardless of rotation, so on three quarters of the corpus it
   *displayed* at the bottom-left, top-right or top-left — nowhere near the
   §1.2 candidate regions. Real title blocks are bottom-right as viewed.

Both are checked through ``PdfBackend`` + the line merge, i.e. through exactly
the path stage B uses, not through PyMuPDF's raw output.
"""

from __future__ import annotations

import pytest

from conduit.bench.make_corpus import DISPLAY_BOTTOM_RIGHT, plan_pages
from conduit.geometry import Point
from conduit.ingest.textlines import merge_page_spans


def _title_block_lines(backend, page: int):
    return [
        line
        for line in merge_page_spans(list(backend.text_spans(page)))
        if "SHEET NUMBER" in line.normalized_text
    ]


def _rotated_title_block_pages(manifest) -> list[int]:
    corrupt = set(manifest.corrupt_pages)
    raster = set(manifest.raster_pages)
    return [
        p["page_number"]
        for p in manifest.pages
        if p["page_number"] % 7 == 0
        and p["page_number"] not in corrupt
        and p["page_number"] not in raster
    ]


def test_the_corpus_has_rotated_title_blocks_to_test(manifest) -> None:
    assert _rotated_title_block_pages(manifest), (
        "no page carries a sideways title block: the rotated case is untested"
    )


def test_sheet_number_is_one_unbroken_token_on_every_vector_page(
    backend, manifest
) -> None:
    corrupt = set(manifest.corrupt_pages)
    raster = set(manifest.raster_pages)
    for spec in manifest.pages:
        page = spec["page_number"]
        if page in corrupt or page in raster:
            continue
        lines = _title_block_lines(backend, page)
        assert len(lines) == 1, f"page {page}: expected one sheet-number line, got {lines}"
        text = lines[0].normalized_text
        assert spec["sheet_number"] in text, (
            f"page {page}: sheet number {spec['sheet_number']!r} is not intact in {text!r} "
            "— the title block wrapped mid-token"
        )


def test_rotated_title_block_text_is_actually_rotated(backend, manifest) -> None:
    """A sideways block must read sideways **on screen**.

    "Rotated" is a display-space property: the stored span rotation is in
    ``pdf_points`` (unrotated page), so a page with ``/Rotate 90`` whose block
    reads sideways on screen stores 180. The subtraction is the whole point —
    getting it wrong is how a corpus ends up "testing" a case it does not
    contain.
    """
    for page in _rotated_title_block_pages(manifest):
        lines = _title_block_lines(backend, page)
        assert lines, f"page {page} has no title block"
        page_rotation = backend.page_geometry(page, dpi=72).rotation_deg
        on_screen = (lines[0].rotation_deg - page_rotation) % 180
        assert on_screen == pytest.approx(90, abs=1.0), (
            f"page {page}: title block at {lines[0].rotation_deg} deg in pdf points on a "
            f"/Rotate {page_rotation} page displays at {on_screen} deg — not sideways"
        )


def test_title_block_displays_at_the_bottom_right(backend, manifest) -> None:
    """§1.2's regions are display-space facts, so the corpus must respect them.

    The sheet-number line is mapped into raster space (``/Rotate`` applied) and
    must land in the right-hand strip or the bottom band of the *rendered*
    page, which is where every candidate region of §1.2 lives.
    """
    corrupt = set(manifest.corrupt_pages)
    raster = set(manifest.raster_pages)
    for spec in manifest.pages:
        page = spec["page_number"]
        if page in corrupt or page in raster:
            continue
        geometry = backend.page_geometry(page, dpi=72)
        line = _title_block_lines(backend, page)[0]
        cx = (line.bbox.x0 + line.bbox.x1) / 2.0
        cy = (line.bbox.y0 + line.bbox.y1) / 2.0
        centre = geometry.pdf_to_raster(Point(cx, cy))
        fx = centre.x / geometry.width_px
        fy = centre.y / geometry.height_px
        assert fx >= 0.82 or fy >= 0.86, (
            f"page {page} (/Rotate {spec['rotation']}): title block displays at "
            f"({fx:.2f}, {fy:.2f}) of the page — not in any §1.2 candidate region"
        )


def test_every_rotation_has_a_placement_rule() -> None:
    rotations = {spec.rotation for spec in plan_pages(24)}
    assert rotations <= set(DISPLAY_BOTTOM_RIGHT), rotations
    assert set(DISPLAY_BOTTOM_RIGHT) == {0, 90, 180, 270}
