"""Locating the title block (``03-pipeline-specs.md`` §1.2).

The three candidate regions of §1.2 are defined in **display space** — what a
person looking at the sheet would call "the right-hand strip" and "the bottom
band". Stored geometry is not in display space: ``TextSpan`` bboxes are
``pdf_points`` of the *unrotated* page. So each candidate is built as a box on
the rendered page and mapped back through ``PageGeometry.raster_bbox_to_pdf``,
which is the transform ``tests/test_geometry.py`` and
``tests/test_pdf_backend_contract.py`` already pin against PyMuPDF's own
rotation matrix.

That single line is §1.6's "the candidate regions are rotated by the same
amount before scoring", implemented by reusing a tested transform instead of
writing a second one.

Nothing here persists: the winning region is used and dropped (§1.2 — there is
no column for a title-block bbox and it is cheap to recompute). It is written
into the ``sheet.classified`` audit payload so a reviewer can see *where* the
sheet number was read from.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from conduit.geometry import BBox, PageGeometry

__all__ = [
    "CANDIDATE_REGIONS",
    "RegionScore",
    "ScoredSpan",
    "candidate_regions",
    "count_axis_aligned_segments",
    "score_region",
]

#: §1.2, expressed as fractions of the **rendered** page (origin top-left,
#: y down), which is how a human describes where a title block sits.
CANDIDATE_REGIONS: tuple[tuple[str, Callable[[float, float], tuple[float, float, float, float]]], ...] = (
    ("right_strip", lambda w, h: (0.82 * w, 0.0, w, h)),
    ("bottom_band", lambda w, h: (0.0, 0.86 * h, w, h)),
    ("right_narrow", lambda w, h: (0.88 * w, 0.0, w, h)),
)

#: A "small text" span, per §1.2.
SMALL_TEXT_PT = 10.0
#: Minimum length of a title-block ruling line, per §1.2.
MIN_RULE_LEN_PT = 36.0
#: 72 x 72 pt per square inch.
PT2_PER_IN2 = 5184.0


@dataclass(frozen=True, slots=True)
class ScoredSpan:
    """The subset of a ``TextSpan`` row stage B actually reads.

    A plain dataclass rather than the ORM row so the scoring is testable
    without a database, and so a session is never needed to reason about a
    page's text.
    """

    id: object
    text: str
    normalized_text: str
    bbox: BBox
    font_size_pt: float | None
    rotation_deg: float = 0.0

    @property
    def center(self) -> tuple[float, float]:
        return ((self.bbox.x0 + self.bbox.x1) / 2.0, (self.bbox.y0 + self.bbox.y1) / 2.0)


@dataclass(frozen=True, slots=True)
class RegionScore:
    name: str
    bbox: BBox
    score: float
    span_count: int
    density: float
    small_fraction: float
    rule_count: int

    def as_payload(self) -> dict:
        return {
            "region": self.name,
            "bbox_pdf_points": [round(v, 2) for v in self.bbox.as_tuple()],
            "score": round(self.score, 4),
            "spans": self.span_count,
            "spans_per_in2": round(self.density, 3),
            "small_text_fraction": round(self.small_fraction, 3),
            "rules": self.rule_count,
        }


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def candidate_regions(geometry: PageGeometry) -> list[tuple[str, BBox]]:
    """The §1.2 candidates for this page, in ``pdf_points``."""
    w_px, h_px = float(geometry.width_px), float(geometry.height_px)
    out: list[tuple[str, BBox]] = []
    for name, make in CANDIDATE_REGIONS:
        x0, y0, x1, y1 = make(w_px, h_px)
        out.append((name, geometry.raster_bbox_to_pdf(BBox(x0, y0, x1, y1))))
    return out


def spans_inside(region: BBox, spans: Sequence[ScoredSpan]) -> list[ScoredSpan]:
    inside = []
    for span in spans:
        cx, cy = span.center
        if region.x0 <= cx <= region.x1 and region.y0 <= cy <= region.y1:
            inside.append(span)
    return inside


def count_axis_aligned_segments(
    paths: Sequence[dict], region: BBox, *, min_len_pt: float = MIN_RULE_LEN_PT
) -> int:
    """§1.2: long axis-aligned rules starting AND ending inside the region.

    ``paths`` are dicts in the ``conduit.page_paths/1`` format written by
    ``conduit.ingest.paths_dump`` — stage B reads the cached dump rather than
    re-opening the PDF (``01-architecture.md`` §A). Curves are ignored: a
    title block is ruled with straight lines.
    """
    count = 0
    for path in paths:
        for item in path.get("items", ()):
            pts = item.get("points", ())
            op = item.get("op")
            if op == "re" and len(pts) >= 4:
                segments = list(zip(pts, [*pts[1:], pts[0]], strict=False))
            elif op in ("l", "qu") and len(pts) >= 2:
                segments = list(zip(pts, pts[1:], strict=False))
            else:
                continue
            for (ax, ay), (bx, by) in segments:
                if not (region.x0 <= ax <= region.x1 and region.y0 <= ay <= region.y1):
                    continue
                if not (region.x0 <= bx <= region.x1 and region.y0 <= by <= region.y1):
                    continue
                dx, dy = abs(bx - ax), abs(by - ay)
                if dy <= 0.5 and dx >= min_len_pt:
                    count += 1
                elif dx <= 0.5 and dy >= min_len_pt:
                    count += 1
    return count


def score_region(
    name: str, region: BBox, spans: Sequence[ScoredSpan], paths: Sequence[dict]
) -> RegionScore:
    """§1.2's ``score_region``, with its ``small`` typo implemented as meant.

    The spec writes ``small = mean(1.0 for s in inside if small) / len(inside)``
    — the mean of a bag of 1.0s is 1.0, so that expression is a constant. The
    intent is unambiguous from the name and the weighting: the fraction of
    spans in the region that are small text.
    """
    inside = spans_inside(region, spans)
    if len(inside) < 6:
        return RegionScore(name, region, 0.0, len(inside), 0.0, 0.0, 0)
    area_in2 = (region.width * region.height) / PT2_PER_IN2
    density = len(inside) / max(area_in2, 1.0)
    small = sum(
        1 for s in inside if (s.font_size_pt if s.font_size_pt else 99.0) <= SMALL_TEXT_PT
    ) / len(inside)
    rules = count_axis_aligned_segments(paths, region)
    score = 0.45 * _clamp(density / 8.0) + 0.30 * small + 0.25 * _clamp(rules / 6.0)
    return RegionScore(name, region, score, len(inside), density, small, rules)
