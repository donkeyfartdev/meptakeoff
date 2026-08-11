"""PDF access. Everything goes through ``PdfBackend`` (risk R9).

``backend`` holds the protocol and the dataclasses; it imports no PDF library.
``pymupdf_backend`` is the one module allowed to import PyMuPDF, and is
imported lazily by ``open_backend`` so that ``conduit.pdf.backend`` stays
usable — and testable — with no PDF library installed at all.
"""

from __future__ import annotations

import os

from conduit.pdf.backend import (
    DocumentInfo,
    PathItem,
    PdfBackend,
    PdfPath,
    PdfTextSpan,
    RenderedPage,
)

__all__ = [
    "DocumentInfo",
    "PathItem",
    "PdfBackend",
    "PdfPath",
    "PdfTextSpan",
    "RenderedPage",
    "backend_name",
    "open_backend",
]

ENV_BACKEND = "CONDUIT_PDF_BACKEND"
DEFAULT_BACKEND = "pymupdf"


def backend_name() -> str:
    """Which backend the profile selects. ``pypdfium2`` is the planned second
    implementation if the AGPL decision goes that way — it becomes a new module
    plus a branch here, and the contract suite must pass unchanged."""
    return os.environ.get(ENV_BACKEND, DEFAULT_BACKEND).strip().lower()


def open_backend(data: bytes, *, filename_hint: str = "document.pdf") -> PdfBackend:
    name = backend_name()
    if name == "pymupdf":
        from conduit.pdf.pymupdf_backend import PyMuPdfBackend

        return PyMuPdfBackend(data, filename_hint=filename_hint)
    from conduit.errors import PdfBackendUnavailable

    raise PdfBackendUnavailable(f"unknown PDF backend {name!r} (set {ENV_BACKEND})")
