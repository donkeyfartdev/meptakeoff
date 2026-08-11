"""PyMuPDF implementation of ``PdfBackend``.

This is the ONLY module in the package that imports PyMuPDF. Everything it
returns is a ``conduit`` dataclass; ``fitz``/``pymupdf`` types never leave.

MuPDF's coordinate space vs ours
--------------------------------
MuPDF reports text and drawing coordinates in the page's **unrotated** space,
y-down, origin at the top-left of the CropBox — ``/Rotate`` is NOT applied
(that is what ``page.rotation_matrix`` is for, and what rendering applies).
Our canonical space is unrotated and y-UP, so the conversion is a y-flip only::

    x_pt = mb.x0 + mx
    y_pt = mb.y1 - my

Verified rather than assumed: ``test_rotated_page_text_is_still_in_unrotated_pdf_points``
fails if a rotation is applied twice (spans land outside the MediaBox), and
``test_transform_matches_mupdf_rotation_matrix`` pins the *rendering* transform,
which does apply the rotation, against MuPDF's own matrix.

``tests/test_pdf_backend_contract.py::test_transform_matches_mupdf`` checks our
independent arithmetic in ``conduit.geometry`` against MuPDF's own
``rotation_matrix`` on every page of the synthetic corpus, so the two
descriptions of the same transform have to agree.

Known gap, stated rather than hidden
------------------------------------
If a page's CropBox differs from its MediaBox, ``page_geometry`` raises
``PageGeometryError`` instead of guessing: MuPDF renders the CropBox, our
``PageGeometry`` is defined on the MediaBox, and the two disagree by the crop
offset. Cropped pages exist in real plan sets; supporting them is a week-2 task
(store the CropBox on ``Sheet`` or render the MediaBox explicitly). Until then
such a page fails loudly as a typed page-level error, and the run continues.

Corruption detection
--------------------
MuPDF is deliberately forgiving: a page whose content stream is unreadable
usually renders blank and *logs* an error rather than raising. So the backend
asks MuPDF what it complained about — ``pymupdf.TOOLS.mupdf_warnings(reset=True)``
— around each page operation, and treats messages matching ``HARD_ERROR_PATTERNS``
as corruption, raising ``CorruptPageError`` with the message attached.

Honest limits of that check: the pattern list was tuned against the synthetic
corpus in ``bench/`` (two deliberately broken pages) plus the errors MuPDF
raises directly. Real plan sets will produce messages this list has never seen,
some benign. The list is data, not logic, so it is meant to be extended as real
sets arrive — and the fallback for an unmatched message is to *accept* the
page, so an unknown warning degrades to "processed, possibly imperfect", never
to a lost run.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from conduit.errors import (
    CorruptPageError,
    PageGeometryError,
    PageRenderError,
    PdfBackendUnavailable,
    PdfOpenError,
)
from conduit.geometry import BBox, PageGeometry, Point
from conduit.pdf.backend import (
    ColorSpace,
    DocumentInfo,
    PathItem,
    PathOp,
    PdfPath,
    PdfTextSpan,
    PixelEncoding,
    RenderedPage,
)

try:  # pragma: no cover - import guard
    import pymupdf  # type: ignore
except ImportError as _exc:  # pragma: no cover - import guard
    raise PdfBackendUnavailable(
        "PyMuPDF is not installed. Install it with `pip install pymupdf`, or "
        "select another PdfBackend implementation."
    ) from _exc


# MuPDF prints parse errors straight to the process stderr from C. We collect
# them programmatically via TOOLS.mupdf_warnings() instead, so turn the printing
# off: a 24-page corpus with two broken pages otherwise emits hundreds of lines
# that no log aggregator can attribute to a page. Set CONDUIT_MUPDF_STDERR=1 to
# get them back while debugging a real plan set.
pymupdf.TOOLS.mupdf_display_errors(bool(os.environ.get("CONDUIT_MUPDF_STDERR")))

BACKEND_NAME = "pymupdf"
BACKEND_VERSION: str = pymupdf.version[0]

#: Substrings that mean "this page's content did not parse", not "this PDF is
#: slightly unusual". Lower-cased comparison. See the module docstring for why
#: this is a permissive list.
HARD_ERROR_PATTERNS: tuple[str, ...] = (
    "cannot find object in xref",
    "object out of range",
    "object out of range",
    "invalid object number",
    "cannot load object",
    "cannot recognize version marker",
    "premature end of data",
    "unexpected end of file",
    "error: cannot",
    "syntax error",
    "zlib error",
    "inflate error",
    "corrupt",
    "broken",
    "cannot decode",
    "not a stream",
    "cycle in page tree",
)

#: Guard against a single render eating the machine (risk R8). 200 dpi on an
#: ARCH E sheet (48x36 in) is 9600x7200 px = 69 Mpx = ~207 MB at 3 channels,
#: which is why stage A renders tiles via ``clip`` rather than whole pages at
#: high DPI. This cap is a backstop, not a policy.
MAX_RENDER_PIXELS = 80_000_000


def _messages(reset: bool = True) -> list[str]:
    try:
        raw = pymupdf.TOOLS.mupdf_warnings(reset=reset)
    except Exception:  # pragma: no cover - very old/very new PyMuPDF
        return []
    if not raw:
        return []
    return [line.strip() for line in str(raw).splitlines() if line.strip()]


def _hard_errors(messages: Sequence[str]) -> list[str]:
    out = []
    for msg in messages:
        low = msg.lower()
        if any(pat in low for pat in HARD_ERROR_PATTERNS):
            out.append(msg)
    return out


class PyMuPdfBackend:
    """``PdfBackend`` over an in-memory PDF.

    Constructed from bytes or from an ``ObjectStore`` key — never from a path,
    so ingest works identically on the local FS and on S3.
    """

    def __init__(self, data: bytes, *, filename_hint: str = "document.pdf") -> None:
        _messages()  # clear anything a previous document left behind
        self._name = filename_hint
        try:
            self._doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise PdfOpenError(f"cannot open {filename_hint!r}: {exc}") from exc
        if self._doc.is_encrypted and self._doc.needs_pass:
            self._doc.close()
            raise PdfOpenError(f"{filename_hint!r} is password-protected")
        self._geom_cache: dict[tuple[int, int], PageGeometry] = {}

    # --- constructors -----------------------------------------------------

    @classmethod
    def from_store(cls, store: Any, key: str) -> PyMuPdfBackend:
        """Open an object out of an ``ObjectStore`` (no filesystem path)."""
        from conduit.store.base import reading

        with reading(store, key) as fp:
            return cls(fp.read(), filename_hint=key)

    # --- lifecycle --------------------------------------------------------

    def close(self) -> None:
        if getattr(self, "_doc", None) is not None and not self._doc.is_closed:
            self._doc.close()

    def __enter__(self) -> PyMuPdfBackend:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # --- document --------------------------------------------------------

    def document_info(self) -> DocumentInfo:
        meta = self._doc.metadata or {}
        return DocumentInfo(
            page_count=self._doc.page_count,
            is_encrypted=bool(self._doc.is_encrypted),
            pdf_version=str(meta.get("format") or "unknown"),
            producer=meta.get("producer") or None,
            title=meta.get("title") or None,
        )

    # --- page access ------------------------------------------------------

    def _load_page(self, page: int) -> Any:
        n = self._doc.page_count
        if page < 1 or page > n:
            raise CorruptPageError(
                f"page {page} out of range (document has {n})", page_number=page
            )
        _messages()
        try:
            p = self._doc.load_page(page - 1)
        except Exception as exc:
            raise CorruptPageError(
                "page object could not be loaded", page_number=page, detail=str(exc)
            ) from exc
        errs = _hard_errors(_messages())
        if errs:
            raise CorruptPageError(
                "page object is damaged", page_number=page, detail="; ".join(errs[:3])
            )
        return p

    def _check_content_stream(self, p: Any, page: int) -> None:
        """Read the raw content stream and fail typed if MuPDF cannot."""
        _messages()
        try:
            xrefs = p.get_contents()
            for xref in xrefs:
                self._doc.xref_stream(xref)  # forces decode of the stream filters
        except Exception as exc:
            raise CorruptPageError(
                "content stream could not be decoded", page_number=page, detail=str(exc)
            ) from exc
        errs = _hard_errors(_messages())
        if errs:
            raise CorruptPageError(
                "content stream is damaged", page_number=page, detail="; ".join(errs[:3])
            )

    def page_geometry(self, page: int, *, dpi: int = 200) -> PageGeometry:
        cached = self._geom_cache.get((page, dpi))
        if cached is not None:
            return cached
        p = self._load_page(page)
        try:
            mb = p.mediabox
            cb = p.cropbox
            rotation = int(p.rotation)
        except Exception as exc:
            raise PageGeometryError(
                "MediaBox/Rotate unreadable", page_number=page, detail=str(exc)
            ) from exc
        if max(abs(cb.x0 - mb.x0), abs(cb.y0 - mb.y0), abs(cb.x1 - mb.x1), abs(cb.y1 - mb.y1)) > 1e-6:
            raise PageGeometryError(
                "CropBox differs from MediaBox — not supported yet, see module docstring",
                page_number=page,
                detail=f"mediabox={tuple(mb)} cropbox={tuple(cb)}",
            )
        geom = PageGeometry(
            page_number=page,
            media_box_x0=float(mb.x0),
            media_box_y0=float(mb.y0),
            media_box_x1=float(mb.x1),
            media_box_y1=float(mb.y1),
            rotation_deg=rotation,
            render_dpi=dpi,
        )
        self._geom_cache[(page, dpi)] = geom
        return geom

    # --- coordinate conversion -------------------------------------------

    def _to_pdf_points(self, geom_72: PageGeometry, x: float, y: float) -> Point:
        """MuPDF page-space point -> canonical pdf_points (y-flip only).

        ``geom_72`` must be the page's geometry with ``rotation_deg=0`` at 72
        dpi — see ``_unrotated`` — because MuPDF's text/drawing coordinates are
        already in unrotated page space.
        """
        return geom_72.raster_to_pdf(Point(x, y))

    @staticmethod
    def _unrotated(geom: PageGeometry) -> PageGeometry:
        return replace(geom, rotation_deg=0, render_dpi=72)

    # --- rendering --------------------------------------------------------

    def render_page(
        self, page: int, *, dpi: int, clip: BBox | None = None, encoding: PixelEncoding = "raw"
    ) -> RenderedPage:
        geom = self.page_geometry(page, dpi=dpi)
        p = self._load_page(page)

        if clip is None:
            want_w, want_h = geom.width_px, geom.height_px
            origin = Point(0.0, 0.0)
            mu_clip = None
        else:
            s = geom.scale
            mu_clip = pymupdf.Rect(clip.x0 / s, clip.y0 / s, clip.x1 / s, clip.y1 / s)
            want_w = max(1, int(round(clip.width)))
            want_h = max(1, int(round(clip.height)))
            origin = Point(clip.x0, clip.y0)

        if want_w * want_h > MAX_RENDER_PIXELS:
            raise PageRenderError(
                f"render of {want_w}x{want_h}px exceeds the {MAX_RENDER_PIXELS}px budget; "
                "render tiles with clip= instead",
                page_number=page,
            )

        _messages()
        try:
            pix = p.get_pixmap(dpi=dpi, clip=mu_clip, alpha=False, annots=False)
        except Exception as exc:
            raise PageRenderError("rasterisation failed", page_number=page, detail=str(exc)) from exc
        errs = _hard_errors(_messages())
        if errs:
            pix = None
            raise CorruptPageError(
                "page could not be rendered cleanly", page_number=page, detail="; ".join(errs[:3])
            )
        try:
            colorspace: ColorSpace = "gray" if pix.n == 1 else "rgb"
            samples = pix.tobytes("png") if encoding == "png" else bytes(pix.samples)
            return RenderedPage(
                page_number=page,
                dpi=dpi,
                width_px=pix.width,
                height_px=pix.height,
                channels=pix.n,
                colorspace=colorspace,
                encoding=encoding,
                samples=samples,
                origin_px=origin,
            )
        finally:
            # Drop the pixmap before returning: one page's pixels live at a
            # time per worker (risk R8).
            del pix

    # --- text -------------------------------------------------------------

    def text_spans(self, page: int) -> Sequence[PdfTextSpan]:
        geom72 = self._unrotated(self.page_geometry(page, dpi=72))
        p = self._load_page(page)
        self._check_content_stream(p, page)
        _messages()
        try:
            raw = p.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT)
        except Exception as exc:
            raise CorruptPageError(
                "text extraction failed", page_number=page, detail=str(exc)
            ) from exc
        errs = _hard_errors(_messages())
        if errs:
            raise CorruptPageError(
                "text extraction hit a damaged object",
                page_number=page,
                detail="; ".join(errs[:3]),
            )

        out: list[PdfTextSpan] = []
        for bi, block in enumerate(raw.get("blocks", [])):
            if block.get("type", 0) != 0:  # 1 == image block
                continue
            for li, line in enumerate(block.get("lines", [])):
                dx, dy = line.get("dir", (1.0, 0.0))
                # A direction vector is a difference of points, so it converts
                # with the rotation only, not the origin: take the difference
                # of two converted points.
                o = self._to_pdf_points(geom72, 0.0, 0.0)
                d = self._to_pdf_points(geom72, float(dx), float(dy))
                direction = Point(d.x - o.x, d.y - o.y)
                for si, span in enumerate(line.get("spans", [])):
                    text = span.get("text", "")
                    if not text.strip():
                        continue
                    x0, y0, x1, y1 = span["bbox"]
                    a = self._to_pdf_points(geom72, float(x0), float(y0))
                    b = self._to_pdf_points(geom72, float(x1), float(y1))
                    flags = int(span.get("flags", 0))
                    out.append(
                        PdfTextSpan(
                            text=text,
                            bbox=BBox.from_points(a, b),
                            font_name=str(span.get("font", "")),
                            font_size=float(span.get("size", 0.0)),
                            block_index=bi,
                            line_index=li,
                            span_index=si,
                            direction=direction,
                            is_bold=bool(flags & 2**4),
                            is_italic=bool(flags & 2**1),
                            color_rgb=int(span.get("color", 0)),
                        )
                    )
        return out

    # --- drawings ---------------------------------------------------------

    _OP_MAP: dict[str, PathOp] = {"l": "l", "c": "c", "re": "re", "qu": "qu"}

    def drawings(self, page: int) -> Sequence[PdfPath]:
        geom72 = self._unrotated(self.page_geometry(page, dpi=72))
        p = self._load_page(page)
        self._check_content_stream(p, page)
        _messages()
        try:
            raw = p.get_drawings()
        except Exception as exc:
            raise CorruptPageError(
                "drawing extraction failed", page_number=page, detail=str(exc)
            ) from exc
        errs = _hard_errors(_messages())
        if errs:
            raise CorruptPageError(
                "drawing extraction hit a damaged object",
                page_number=page,
                detail="; ".join(errs[:3]),
            )

        def cvt(pt: Any) -> Point:
            return self._to_pdf_points(geom72, float(pt.x), float(pt.y))

        out: list[PdfPath] = []
        for seq, d in enumerate(raw):
            items: list[PathItem] = []
            for item in d.get("items", []):
                op = str(item[0])
                mapped = self._OP_MAP.get(op)
                if mapped is None:
                    continue
                if mapped == "re":
                    r = item[1]
                    pts = (
                        cvt(pymupdf.Point(r.x0, r.y0)),
                        cvt(pymupdf.Point(r.x1, r.y0)),
                        cvt(pymupdf.Point(r.x1, r.y1)),
                        cvt(pymupdf.Point(r.x0, r.y1)),
                    )
                elif mapped == "qu":
                    q = item[1]
                    pts = (cvt(q.ul), cvt(q.ur), cvt(q.lr), cvt(q.ll))
                else:
                    pts = tuple(cvt(pt) for pt in item[1:])
                items.append(PathItem(op=mapped, points=pts))
            if not items:
                continue
            r = d.get("rect")
            bbox = BBox.from_points(cvt(pymupdf.Point(r.x0, r.y0)), cvt(pymupdf.Point(r.x1, r.y1)))
            kind_raw = str(d.get("type", "s"))
            kind = {"s": "stroke", "f": "fill", "fs": "stroke_fill", "clip": "clip"}.get(
                kind_raw, "stroke"
            )
            dash = str(d.get("dashes") or "")
            out.append(
                PdfPath(
                    items=tuple(items),
                    kind=kind,  # type: ignore[arg-type]
                    bbox=bbox,
                    line_width=float(d.get("width") or 0.0),
                    stroke_color=tuple(float(c) for c in d["color"]) if d.get("color") else None,
                    fill_color=tuple(float(c) for c in d["fill"]) if d.get("fill") else None,
                    dashes="" if dash in ("[] 0", "") else dash,
                    closed=bool(d.get("closePath", False)),
                    even_odd=bool(d.get("even_odd", False)),
                    layer=d.get("layer") or None,
                    seq=seq,
                )
            )
        return out
