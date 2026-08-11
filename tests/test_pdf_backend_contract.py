"""The ``PdfBackend`` contract.

Two jobs:

1. **Guard the AGPL escape hatch (risk R9).** ``conduit/pdf/backend.py`` must
   not import a PDF library and must not mention a PDF library type in any
   signature. If that ever stops being true, swapping PyMuPDF for pypdfium2
   stops being "write a class" and becomes a refactor — which is exactly the
   cost the seam exists to avoid. These tests fail loudly rather than let it
   drift.
2. **Behaviour every implementation must have**, written against the protocol
   rather than against ``PyMuPdfBackend``, so a second backend is tested by
   pointing this module at it.
"""

from __future__ import annotations

import inspect
import re
import typing
from pathlib import Path

import pytest

from conduit.geometry import BBox, Point
from conduit.pdf import backend as backend_mod
from conduit.pdf.backend import PdfBackend, PdfPath, PdfTextSpan, RenderedPage

PDF_LIBRARY_TOKENS = ("fitz", "pymupdf", "mupdf", "pypdfium", "pdfium", "pikepdf", "pypdf")

ALLOWED_ANNOTATION_MODULES = {"builtins", "typing", "collections.abc", "types", "NoneType"}


def test_backend_module_does_not_import_a_pdf_library() -> None:
    src = Path(backend_mod.__file__).read_text()
    # Strip docstrings and comments, which legitimately discuss PyMuPDF.
    code_lines, in_doc = [], False
    for line in src.splitlines():
        stripped = line.strip()
        if in_doc:
            if stripped.endswith('"""'):
                in_doc = False
            continue
        if stripped.startswith('"""'):
            if not (stripped.endswith('"""') and len(stripped) > 3):
                in_doc = True
            continue
        code_lines.append(line.split("#", 1)[0])
    code = "\n".join(code_lines)
    for token in PDF_LIBRARY_TOKENS:
        assert not re.search(rf"\b{token}\b", code, flags=re.I), (
            f"{token!r} appears in executable code of conduit/pdf/backend.py — "
            "the backend seam must stay library-free"
        )


def test_no_pdf_library_type_in_any_protocol_signature() -> None:
    """Resolve every annotation on every protocol method and check its origin."""
    ns = vars(backend_mod)
    checked = 0
    for name, member in inspect.getmembers(PdfBackend, inspect.isfunction):
        if name.startswith("__") and name not in ("__enter__", "__exit__"):
            continue
        hints = typing.get_type_hints(member, globalns=ns)
        for param, hint in hints.items():
            for part in _flatten(hint):
                mod = getattr(part, "__module__", None)
                assert mod is None or mod.startswith("conduit") or mod in ALLOWED_ANNOTATION_MODULES, (
                    f"PdfBackend.{name}({param}) is annotated with {part!r} from {mod!r}"
                )
                checked += 1
    assert checked > 10, "annotation walk found suspiciously little to check"


def _flatten(hint: object) -> list[object]:
    args = typing.get_args(hint)
    if not args:
        return [hint]
    out: list[object] = []
    origin = typing.get_origin(hint)
    if origin is not None:
        out.append(origin)
    for a in args:
        out.extend(_flatten(a))
    return out


def test_dataclasses_returned_by_the_seam_are_ours() -> None:
    for cls in (RenderedPage, PdfTextSpan, PdfPath):
        assert cls.__module__.startswith("conduit")


def test_implementation_satisfies_the_protocol(backend) -> None:
    assert isinstance(backend, PdfBackend)
    from conduit.pdf.pymupdf_backend import PyMuPdfBackend

    for name in ("document_info", "page_geometry", "render_page", "text_spans", "drawings"):
        assert callable(getattr(PyMuPdfBackend, name))


# --- behaviour ------------------------------------------------------------


def test_document_info(backend, manifest) -> None:
    info = backend.document_info()
    assert info.page_count == manifest.page_count
    assert info.is_encrypted is False
    assert info.pdf_version.startswith("PDF")


def test_transform_matches_mupdf_rotation_matrix(backend, manifest) -> None:
    """Independent check of ``conduit.geometry`` against MuPDF's own matrix.

    MuPDF's ``page.rotation_matrix`` maps unrotated page space (y down, origin
    at the MediaBox top-left) to the rotated space it reports coordinates in.
    Our ``pdf_to_raster`` at 72 dpi must land on the same point, having gone
    through completely separate arithmetic.
    """
    import pymupdf  # test-only import; the pipeline never does this

    doc = pymupdf.open(stream=backend._doc.tobytes(), filetype="pdf")
    try:
        for page_no in range(1, manifest.page_count + 1):
            g = backend.page_geometry(page_no, dpi=72)
            p = doc.load_page(page_no - 1)
            for ux, uy in [(0.0, 0.0), (137.5, 42.25), (g.width_pt, g.height_pt)]:
                mu = pymupdf.Point(ux, uy) * p.rotation_matrix
                ours = g.pdf_to_raster(
                    Point(g.media_box_x0 + ux, g.media_box_y1 - uy)
                )
                assert abs(ours.x - mu.x) < 0.01, f"page {page_no} x"
                assert abs(ours.y - mu.y) < 0.01, f"page {page_no} y"
    finally:
        doc.close()


def test_render_page_dimensions_match_geometry(backend) -> None:
    g = backend.page_geometry(1, dpi=72)
    rendered = backend.render_page(1, dpi=72)
    assert (rendered.width_px, rendered.height_px) == (g.width_px, g.height_px)
    assert rendered.channels == 3
    assert rendered.nbytes == rendered.width_px * rendered.height_px * rendered.channels
    assert rendered.encoding == "raw"


def test_render_clip_is_a_subrender_in_raster_px(backend) -> None:
    clip = BBox(100.0, 50.0, 356.0, 306.0)  # 256 x 256 px at the requested dpi
    tile = backend.render_page(1, dpi=72, clip=clip)
    assert (tile.width_px, tile.height_px) == (256, 256)
    assert tile.origin_px == Point(100.0, 50.0)
    assert tile.nbytes < 256 * 256 * 3 + 1


def test_render_png_encoding(backend) -> None:
    png = backend.render_page(1, dpi=36, encoding="png")
    assert png.encoding == "png"
    assert png.samples[:8] == b"\x89PNG\r\n\x1a\n"


def test_oversized_render_is_refused_not_attempted(backend) -> None:
    from conduit.errors import PageRenderError

    with pytest.raises(PageRenderError):
        backend.render_page(1, dpi=1200)


def test_text_spans_are_in_pdf_points_and_inside_the_page(backend) -> None:
    spans = backend.text_spans(1)
    assert spans, "vector page 1 must have text"
    g = backend.page_geometry(1)
    mb = g.media_box()
    for s in spans:
        assert isinstance(s, PdfTextSpan)
        assert mb.x0 - 1 <= s.bbox.x0 <= s.bbox.x1 <= mb.x1 + 1
        assert mb.y0 - 1 <= s.bbox.y0 <= s.bbox.y1 <= mb.y1 + 1
        assert s.font_size > 0


def test_title_block_text_is_recoverable(backend) -> None:
    text = " ".join(s.text for s in backend.text_spans(1))
    assert "SHEET NUMBER" in text
    assert "E-101" in text
    assert "LEGEND" in text


def test_rotated_page_text_is_still_in_unrotated_pdf_points(backend, manifest) -> None:
    rotated = [
        p["page_number"]
        for p in manifest.pages
        if p["rotation"] == 90 and p["kind"] == "vector"
    ]
    assert rotated, "corpus should contain a rotated vector page"
    page = rotated[0]
    g = backend.page_geometry(page)
    mb = g.media_box()
    for s in backend.text_spans(page):
        # pdf_points are unrotated, so boxes stay inside the *unrotated* box
        # even though the rendered page is landscape-flipped.
        assert mb.x0 - 1 <= s.bbox.x0 <= mb.x1 + 1
        assert mb.y0 - 1 <= s.bbox.y0 <= mb.y1 + 1


def test_drawings_are_paths_in_pdf_points(backend) -> None:
    paths = backend.drawings(1)
    assert paths, "vector page 1 must have drawings"
    g = backend.page_geometry(1)
    mb = g.media_box()
    assert any(p.kind == "stroke" for p in paths)
    assert any(len(p.items) >= 1 for p in paths)
    for p in paths:
        assert isinstance(p, PdfPath)
        assert mb.x0 - 2 <= p.bbox.x0 <= p.bbox.x1 <= mb.x1 + 2
        assert mb.y0 - 2 <= p.bbox.y0 <= p.bbox.y1 <= mb.y1 + 2


def test_dashed_run_is_reported_with_its_dash_pattern(backend) -> None:
    assert any(p.dashes for p in backend.drawings(1)), "the dashed run should survive the seam"


def test_flattened_raster_page_has_no_vector_content(backend, manifest) -> None:
    """The 'scanned sheet' case: no text spans at all, which is what week 1
    records and skips rather than silently treating as an empty sheet."""
    assert manifest.raster_pages
    for page in manifest.raster_pages:
        assert backend.text_spans(page) == []


def test_page_out_of_range_is_typed(backend, manifest) -> None:
    from conduit.errors import CorruptPageError

    with pytest.raises(CorruptPageError):
        backend.page_geometry(manifest.page_count + 5)
