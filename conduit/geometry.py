"""Coordinate spaces and the transforms between them.

This module is pure arithmetic: it imports no PDF library and no ORM. It is the
vocabulary that ``conduit.pdf.backend`` speaks, which is what keeps PyMuPDF
types out of every signature in the codebase (risk R9).

Two spaces, defined exactly as in ``conduit/db/models.py``:

``pdf_points``
    PDF user space of the **unrotated** page. 72 units per inch, origin at the
    MediaBox lower-left, y increasing UP. DPI-independent, and therefore the
    canonical space for everything derived from vector content (``TextSpan``,
    ``ScheduleTable``, ``Measurement``).

``raster_px``
    Pixels of the rendered page image, origin TOP-LEFT, y increasing DOWN, at
    exactly ``render_dpi``, **after** ``/Rotate`` has been applied — i.e. what
    a viewer shows and what a detector sees. ``Detection`` boxes live here.

Forward transform (pdf_points -> raster_px), with ``s = dpi / 72``::

    u = x_pt - mb.x0                 # left offset,  points, unrotated
    v = mb.y1 - y_pt                 # top  offset,  points, unrotated (y flip)
    W = mb.x1 - mb.x0 ; H = mb.y1 - mb.y0

    rot   0:  (u,      v    )        image is W x H points
    rot  90:  (H - v,  u    )        image is H x W points   (clockwise)
    rot 180:  (W - u,  H - v)        image is W x H points
    rot 270:  (v,      W - u)        image is H x W points   (anticlockwise)

    (x_px, y_px) = (x' * s, y' * s)

The 90/270 cases are the ones people get wrong, so
``tests/test_geometry.py::test_rotation_corner_mapping`` pins the corner
mapping explicitly, and ``tests/test_pdf_backend_contract.py`` cross-checks the
whole transform against PyMuPDF's own ``rotation_matrix`` on the synthetic
corpus. That cross-check is the reason to trust this table rather than the
comment above it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from conduit.errors import PageGeometryError, UnsupportedRotationError

__all__ = [
    "BBox",
    "PageGeometry",
    "Point",
    "normalize_rotation",
]


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass(frozen=True, slots=True)
class BBox:
    """Axis-aligned box. Always stored normalised: x0 <= x1 and y0 <= y1.

    The same class is used for both spaces; which space a given box is in is a
    property of the field that holds it, and every persisted box in the schema
    says so in its column name (``bbox_*`` on ``Detection`` is raster_px, on
    ``TextSpan`` it is pdf_points).
    """

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if self.x1 < self.x0 or self.y1 < self.y0:
            object.__setattr__(self, "x0", min(self.x0, self.x1))
            object.__setattr__(self, "x1", max(self.x0, self.x1))
            object.__setattr__(self, "y0", min(self.y0, self.y1))
            object.__setattr__(self, "y1", max(self.y0, self.y1))

    @classmethod
    def from_points(cls, a: Point, b: Point) -> BBox:
        return cls(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


def normalize_rotation(rotation_deg: int) -> int:
    """Fold any ``/Rotate`` value into {0, 90, 180, 270}.

    Raises ``UnsupportedRotationError`` if it is not a multiple of 90; the PDF
    spec requires multiples of 90, and a page that violates it is a page whose
    geometry we do not trust.
    """
    if rotation_deg % 90 != 0:
        raise UnsupportedRotationError(
            f"/Rotate {rotation_deg} is not a multiple of 90", detail=str(rotation_deg)
        )
    return rotation_deg % 360


@dataclass(frozen=True, slots=True)
class PageGeometry:
    """Everything needed to convert coordinates for one page, and nothing else.

    Field names deliberately mirror ``Sheet`` columns in ``conduit/db/models.py``
    (``media_box_x0`` .. ``media_box_y1``, ``rotation_deg``, ``render_dpi``,
    ``width_px``, ``height_px``) so stage A can persist this object field for
    field without a translation layer.
    """

    page_number: int  # 1-based, matches Sheet.page_number
    media_box_x0: float
    media_box_y0: float
    media_box_x1: float
    media_box_y1: float
    rotation_deg: int
    render_dpi: int = 200

    def __post_init__(self) -> None:
        if self.media_box_x1 <= self.media_box_x0 or self.media_box_y1 <= self.media_box_y0:
            raise PageGeometryError(
                "degenerate MediaBox", page_number=self.page_number, detail=str(self.media_box())
            )
        if self.render_dpi < 36 or self.render_dpi > 1200:
            # Mirrors ck_sheet_dpi in the ORM.
            raise PageGeometryError(
                f"render_dpi {self.render_dpi} outside 36..1200", page_number=self.page_number
            )
        object.__setattr__(self, "rotation_deg", normalize_rotation(self.rotation_deg))

    # --- basic dimensions -------------------------------------------------

    def media_box(self) -> BBox:
        return BBox(self.media_box_x0, self.media_box_y0, self.media_box_x1, self.media_box_y1)

    @property
    def width_pt(self) -> float:
        """Unrotated page width in points."""
        return self.media_box_x1 - self.media_box_x0

    @property
    def height_pt(self) -> float:
        """Unrotated page height in points."""
        return self.media_box_y1 - self.media_box_y0

    @property
    def scale(self) -> float:
        return self.render_dpi / 72.0

    @property
    def rotated_width_pt(self) -> float:
        return self.height_pt if self.rotation_deg in (90, 270) else self.width_pt

    @property
    def rotated_height_pt(self) -> float:
        return self.width_pt if self.rotation_deg in (90, 270) else self.height_pt

    @property
    def width_px(self) -> int:
        """Rendered image width. ``ceil`` matches MuPDF's pixmap sizing."""
        return int(math.ceil(round(self.rotated_width_pt * self.scale, 6)))

    @property
    def height_px(self) -> int:
        return int(math.ceil(round(self.rotated_height_pt * self.scale, 6)))

    def at_dpi(self, dpi: int) -> PageGeometry:
        """Same page, different render DPI. Useful for tile sub-renders."""
        return PageGeometry(
            page_number=self.page_number,
            media_box_x0=self.media_box_x0,
            media_box_y0=self.media_box_y0,
            media_box_x1=self.media_box_x1,
            media_box_y1=self.media_box_y1,
            rotation_deg=self.rotation_deg,
            render_dpi=dpi,
        )

    # --- point transforms -------------------------------------------------

    def pdf_to_raster(self, p: Point) -> Point:
        u = p.x - self.media_box_x0
        v = self.media_box_y1 - p.y
        w, h = self.width_pt, self.height_pt
        rot = self.rotation_deg
        if rot == 0:
            xr, yr = u, v
        elif rot == 90:
            xr, yr = h - v, u
        elif rot == 180:
            xr, yr = w - u, h - v
        else:  # 270
            xr, yr = v, w - u
        s = self.scale
        return Point(xr * s, yr * s)

    def raster_to_pdf(self, p: Point) -> Point:
        s = self.scale
        xr, yr = p.x / s, p.y / s
        w, h = self.width_pt, self.height_pt
        rot = self.rotation_deg
        if rot == 0:
            u, v = xr, yr
        elif rot == 90:
            u, v = yr, h - xr
        elif rot == 180:
            u, v = w - xr, h - yr
        else:  # 270
            u, v = w - yr, xr
        return Point(self.media_box_x0 + u, self.media_box_y1 - v)

    # --- box transforms ---------------------------------------------------

    def pdf_bbox_to_raster(self, box: BBox) -> BBox:
        a = self.pdf_to_raster(Point(box.x0, box.y0))
        b = self.pdf_to_raster(Point(box.x1, box.y1))
        return BBox.from_points(a, b)

    def raster_bbox_to_pdf(self, box: BBox) -> BBox:
        a = self.raster_to_pdf(Point(box.x0, box.y0))
        b = self.raster_to_pdf(Point(box.x1, box.y1))
        return BBox.from_points(a, b)

    # --- lengths ----------------------------------------------------------

    def px_to_pt(self, length_px: float) -> float:
        """Lengths are rotation-invariant; only the scale factor applies."""
        return length_px / self.scale

    def pt_to_px(self, length_pt: float) -> float:
        return length_pt * self.scale
