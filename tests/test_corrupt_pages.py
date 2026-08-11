"""Corrupt pages must fail typed, per page, without taking the run down.

The corpus contains exactly two deliberately broken pages:

* ``/Contents`` pointing at an object that is not in the xref;
* a content stream replaced with garbage operators.

MuPDF is forgiving by design — it will happily render a blank page and only
*log* the problem — so these tests are also what proves the backend's
corruption detection is actually wired up, not just declared.
"""

from __future__ import annotations

import pytest

from conduit.errors import ConduitError, CorruptPageError, PageLevelError


def test_manifest_declares_two_corrupt_pages(manifest) -> None:
    assert len(manifest.corrupt_pages) == 2


@pytest.mark.parametrize("op", ["text_spans", "drawings", "render_page"])
def test_corrupt_pages_raise_typed_page_level_errors(backend, manifest, op: str) -> None:
    for page in manifest.corrupt_pages:
        with pytest.raises(CorruptPageError) as excinfo:
            if op == "render_page":
                backend.render_page(page, dpi=36)
            else:
                getattr(backend, op)(page)
        err = excinfo.value
        assert isinstance(err, PageLevelError)
        assert err.page_number == page
        assert err.detail, "a corrupt-page error must carry the underlying reason"


def test_corruption_does_not_break_the_rest_of_the_run(backend, manifest) -> None:
    """A whole-document sweep: corrupt pages are recorded, others succeed.

    This is the shape stage A's per-page loop takes — the failure list is what
    becomes ``PageTaskState(status='failed')`` rows.
    """
    ok: list[int] = []
    failed: dict[int, str] = {}
    for page in range(1, manifest.page_count + 1):
        try:
            backend.text_spans(page)
            backend.drawings(page)
        except PageLevelError as exc:
            failed[page] = type(exc).__name__
            continue
        ok.append(page)

    assert sorted(failed) == sorted(manifest.corrupt_pages)
    assert len(ok) == manifest.page_count - len(manifest.corrupt_pages)
    assert set(failed.values()) == {"CorruptPageError"}


def test_geometry_still_readable_on_a_corrupt_page(backend, manifest) -> None:
    """Page geometry comes from the page dictionary, not its content stream.

    That matters: a sheet whose content is unreadable still gets a ``Sheet`` row
    with a real MediaBox and rotation, so the failure is attributable to a page
    rather than to a hole in the page sequence.
    """
    for page in manifest.corrupt_pages:
        g = backend.page_geometry(page)
        assert g.width_pt > 0 and g.height_pt > 0


def test_typed_errors_are_all_conduit_errors() -> None:
    assert issubclass(CorruptPageError, PageLevelError)
    assert issubclass(PageLevelError, ConduitError)


def test_opening_a_non_pdf_is_a_document_level_error() -> None:
    from conduit.errors import PdfOpenError
    from conduit.pdf.pymupdf_backend import PyMuPdfBackend

    with pytest.raises(PdfOpenError):
        PyMuPdfBackend(b"this is not a pdf", filename_hint="junk.bin")
