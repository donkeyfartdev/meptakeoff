"""``PdfBackend`` — the seam that isolates the PDF library from the codebase.

WHY THIS FILE IS STRICT
=======================
PyMuPDF is AGPL (risk R9 in ``06-risks.md``). The licence decision belongs to
the owner and has been escalated; what engineering guarantees is that the
decision stays *cheap* — swapping to ``pypdfium2`` must be "write a second
class and run the same test suite", not a refactor of every call site.

Therefore, the rule enforced by ``tests/test_pdf_backend_contract.py``:

    **No PyMuPDF type may appear in any signature in this module, and this
    module must not import PyMuPDF (or any other PDF library) at all.**

Everything crossing the seam is a dataclass defined here or in
``conduit.geometry``. ``fitz.Page``, ``fitz.Rect``, ``fitz.Pixmap``,
``fitz.Matrix`` stop at ``conduit/pdf/pymupdf_backend.py``. The test asserts
this by (a) scanning this module's source for any reference to a PDF library
and (b) resolving every annotation on every protocol method and asserting each
one comes from ``conduit``, ``builtins``, ``typing`` or ``collections.abc``.

COORDINATE CONTRACT
===================
* ``text_spans`` and ``drawings`` return geometry in **pdf_points**: unrotated
  page space, origin at the MediaBox lower-left, y UP. This is the canonical
  space of ``conduit/db/models.py``. Backends are responsible for undoing
  whatever space their library natively uses — MuPDF's is rotated and y-down,
  so the PyMuPDF backend converts.
* ``render_page`` returns pixels in **raster_px**: origin top-left, y down,
  ``/Rotate`` applied, at the requested DPI. ``clip`` is expressed in raster_px
  at that same DPI, because tiles are defined on the raster grid.
* ``page`` arguments are 1-based everywhere, matching ``Sheet.page_number``.

FAILURE CONTRACT
================
Page-level problems raise ``conduit.errors.PageLevelError`` subclasses
(``CorruptPageError``, ``PageRenderError``, ``PageGeometryError``); the caller
records the page as failed and the run continues. Document-level problems raise
``PdfOpenError``. A backend must never let a raw library exception escape.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from conduit.geometry import BBox, PageGeometry, Point

__all__ = [
    "DocumentInfo",
    "PathItem",
    "PdfBackend",
    "PdfPath",
    "PdfTextSpan",
    "RenderedPage",
]

PixelEncoding = Literal["raw", "png"]
ColorSpace = Literal["rgb", "gray"]
PathOp = Literal["l", "c", "re", "qu"]
PathKind = Literal["stroke", "fill", "stroke_fill", "clip"]


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    """Whole-document facts obtainable without rendering anything."""

    page_count: int
    is_encrypted: bool
    pdf_version: str
    producer: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """A rasterised page or clip of one.

    ``samples`` is either raw interleaved bytes (``encoding="raw"``, length
    ``width_px * height_px * channels``) or an encoded image (``encoding="png"``).
    Callers stream this straight into an ``ObjectStore``; nothing keeps more
    than one of these alive per worker (risk R8).
    """

    page_number: int
    dpi: int
    width_px: int
    height_px: int
    channels: int
    colorspace: ColorSpace
    encoding: PixelEncoding
    samples: bytes
    origin_px: Point  # top-left of this render within the full page raster

    @property
    def nbytes(self) -> int:
        return len(self.samples)


@dataclass(frozen=True, slots=True)
class PdfTextSpan:
    """One run of text with a single font/size, in pdf_points.

    Indices are the source library's block/line/span ordering, kept so the
    line-merge of ``03-pipeline-specs.md`` §2.2 can group spans deterministically.
    ``direction`` is the unit writing direction in pdf_points, which is how
    rotated title-block text is detected without rasterising anything.
    """

    text: str
    bbox: BBox
    font_name: str
    font_size: float
    block_index: int
    line_index: int
    span_index: int
    direction: Point = Point(1.0, 0.0)
    is_bold: bool = False
    is_italic: bool = False
    color_rgb: int = 0

    @property
    def is_horizontal(self) -> bool:
        return abs(self.direction.y) < 1e-6 and self.direction.x > 0


@dataclass(frozen=True, slots=True)
class PathItem:
    """One primitive of a path. ``op`` follows MuPDF's vocabulary, which
    pdfium can also produce: line, cubic bezier, rectangle, quad."""

    op: PathOp
    points: tuple[Point, ...]


@dataclass(frozen=True, slots=True)
class PdfPath:
    """A vector drawing operation, in pdf_points.

    Stage E classifies these into conduit/pipe/duct runs, so the attributes it
    keys on — ``line_width``, ``dashes``, ``kind``, ``color`` — are all carried
    across the seam rather than re-derived from a raster.
    """

    items: tuple[PathItem, ...]
    kind: PathKind
    bbox: BBox
    line_width: float = 0.0
    stroke_color: tuple[float, ...] | None = None
    fill_color: tuple[float, ...] | None = None
    dashes: str = ""
    closed: bool = False
    even_odd: bool = False
    layer: str | None = None  # OCG name, when the PDF has optional content
    seq: int = 0  # draw order within the page
    extra: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class PdfBackend(Protocol):
    """Every PDF read in this codebase goes through one of these.

    Implementations are constructed from bytes or from an ``ObjectStore`` key —
    never from a filesystem path, so the same code works on S3. They are
    context managers, and are single-document and single-threaded by contract.
    """

    def document_info(self) -> DocumentInfo:
        """Page count and header facts, without rendering or parsing content."""
        ...

    def page_geometry(self, page: int, *, dpi: int = 200) -> PageGeometry:
        """MediaBox + ``/Rotate`` + raster dimensions for one page.

        Cheap: must not rasterise. Raises ``PageGeometryError`` if the page's
        boxes are unusable.
        """
        ...

    def render_page(
        self, page: int, *, dpi: int, clip: BBox | None = None, encoding: PixelEncoding = "raw"
    ) -> RenderedPage:
        """Rasterise a page, or the ``clip`` sub-rectangle of it (raster_px).

        Raises ``PageRenderError`` on failure, including when the requested
        render would exceed the backend's pixel budget.
        """
        ...

    def text_spans(self, page: int) -> Sequence[PdfTextSpan]:
        """Vector text of the page in pdf_points. Empty for a scanned page."""
        ...

    def drawings(self, page: int) -> Sequence[PdfPath]:
        """Vector paths of the page in pdf_points."""
        ...

    def close(self) -> None: ...

    def __enter__(self) -> PdfBackend: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...


def iter_pages(backend: PdfBackend) -> Iterator[int]:
    """1-based page numbers. Streaming by page is the memory contract (R8)."""
    return iter(range(1, backend.document_info().page_count + 1))
