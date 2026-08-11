"""Typed errors.

Rule: a page that cannot be processed must fail with one of these, carrying the
identifiers needed to write an ``AuditEvent`` / ``PageTaskState`` row — never
with a bare library exception, and never by taking the run down. Stage code
catches ``PageLevelError`` per page, records it, and continues; it catches
``DocumentLevelError`` and fails the run.
"""

from __future__ import annotations


class ConduitError(Exception):
    """Base for every error this codebase raises deliberately."""


class DocumentLevelError(ConduitError):
    """The whole document is unusable; the run cannot continue."""


class PageLevelError(ConduitError):
    """One page is unusable. The run records it and continues.

    ``page_number`` is 1-based, matching ``Sheet.page_number``.
    """

    def __init__(self, message: str, *, page_number: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.page_number = page_number
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - formatting only
        base = super().__str__()
        parts = [base]
        if self.page_number is not None:
            parts.append(f"(page {self.page_number})")
        if self.detail:
            parts.append(f"detail={self.detail!r}")
        return " ".join(parts)


# --- document level -------------------------------------------------------


class PdfOpenError(DocumentLevelError):
    """The file is not a PDF, is encrypted, or the xref is unrecoverable."""


class PdfBackendUnavailable(DocumentLevelError):
    """The configured PDF backend could not be imported/initialised."""


# --- page level -----------------------------------------------------------


class CorruptPageError(PageLevelError):
    """The page exists but its content could not be parsed cleanly.

    Raised when the backend's per-page corruption check trips: a missing or
    unreadable content stream, a broken page object, or MuPDF reporting a hard
    parse error while reading the page. See
    ``conduit.pdf.pymupdf_backend`` for exactly what is treated as corruption
    and the honest limits of that check.
    """


class PageRenderError(PageLevelError):
    """Rasterisation of the page (or of a clip of it) failed."""


class PageGeometryError(PageLevelError):
    """The page's MediaBox/Rotate could not be read, or is degenerate."""


class UnsupportedRotationError(PageGeometryError):
    """``/Rotate`` is not a multiple of 90 after normalisation."""


class ObjectStoreError(ConduitError):
    """Object store read/write failed, or an integrity check failed."""


class ObjectNotFound(ObjectStoreError):
    """No object with that key exists in the store."""
