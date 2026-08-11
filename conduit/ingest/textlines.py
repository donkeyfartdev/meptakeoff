"""Span -> line merge (``03-pipeline-specs.md`` §2.2) and text normalisation.

Why this exists: PyMuPDF's own block/line grouping is unreliable on CAD output
— a dimension string routinely arrives as five separate glyph runs. The spec
rebuilds lines from geometry, and **persists one ``TextSpan`` row per merged
line**, not per glyph run (§2.2, "Persistence decision"): a 200-sheet set is
~200k merged spans, and per-glyph persistence is 5-10x that for no analytic
benefit. Raw glyph runs are one ``get_text()`` call away from the immutable
original PDF if they are ever needed again.

The spec's rule, verbatim in intent:

    same_baseline = |Δ baseline| <= 0.30 * em
    same_angle    = |Δ angle|    <= 2.0 degrees
    gap           = next.start - prev.end <= 1.5 * em     (space if > 0.6 em)

with ``em = font_size or 8.0``.

Generalised for rotated text, because plan sheets have plenty of it
-------------------------------------------------------------------
The spec is written in x/y for horizontal text. Applying it literally to a
title block set at 90 degrees would merge nothing. So each span is projected
into its own writing frame first: ``along`` is the coordinate in the writing
direction, ``across`` is perpendicular to it. For horizontal text
(``direction = (1, 0)``) ``along`` is x and ``across`` is y, and the rule is
exactly the spec's. Spans are grouped by angle before merging, so vertical and
horizontal text never merge into one line.

One documented addition to the spec's three conditions
------------------------------------------------------
The spec merges on baseline, angle and gap. Running it over the synthetic
corpus's rotated title block showed that this merges *across fields*: a 12 pt
"SHEET NUMBER:" fragment and a 10 pt sheet-title fragment that happen to share
a baseline and sit 7 pt apart became one line ("MECHASH"). Two runs of text at
different font sizes are not one logical line on any drawing anyone has ever
issued, so a fourth condition is applied — ``|Δ em| <= 0.10 * max(em)``. It is
listed here rather than buried because it is a deviation from `03` §2.2, and
``MERGE_VERSION`` moved when it was added.

``MERGE_VERSION`` travels on ``PipelineRun.model_versions["text"]`` and is what
populates ``EvidenceRef.extractor_version`` for text evidence: changing the
merge changes the version, so old rows stay attributable to the code that made
them.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

from conduit.geometry import BBox
from conduit.pdf.backend import PdfTextSpan

__all__ = [
    "MERGE_VERSION",
    "MergedLine",
    "merge_page_spans",
    "normalize_text",
]

#: Bump whenever the merge or the normaliser changes behaviour.
MERGE_VERSION = "linemerge-2"

BASELINE_TOLERANCE_EM = 0.30
ANGLE_TOLERANCE_DEG = 2.0
JOIN_GAP_EM = 1.5
SPACE_GAP_EM = 0.6
FONT_SIZE_TOLERANCE = 0.10
DEFAULT_EM = 8.0


@dataclass(frozen=True, slots=True)
class MergedLine:
    """One logical line of text, in ``pdf_points``."""

    text: str
    normalized_text: str
    bbox: BBox
    rotation_deg: float
    font_name: str
    font_size_pt: float
    span_count: int
    block_index: int


@dataclass(frozen=True, slots=True)
class _Projected:
    span: PdfTextSpan
    angle: float
    along0: float
    along1: float
    across: float

    @property
    def em(self) -> float:
        return self.span.font_size or DEFAULT_EM


def _angle_of(span: PdfTextSpan) -> float:
    d = span.direction
    if abs(d.x) < 1e-9 and abs(d.y) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(d.y, d.x))


def _project(span: PdfTextSpan) -> _Projected:
    """Express the span's bbox in its own writing frame.

    ``along`` runs with the text; ``across`` is the baseline offset. For
    horizontal text this returns (x0, x1, y0) — i.e. the spec's own variables.
    """
    angle = _angle_of(span)
    rad = math.radians(angle)
    ux, uy = math.cos(rad), math.sin(rad)
    nx, ny = -uy, ux
    corners = (
        (span.bbox.x0, span.bbox.y0),
        (span.bbox.x1, span.bbox.y0),
        (span.bbox.x0, span.bbox.y1),
        (span.bbox.x1, span.bbox.y1),
    )
    alongs = [x * ux + y * uy for x, y in corners]
    acrosses = [x * nx + y * ny for x, y in corners]
    return _Projected(
        span=span,
        angle=angle,
        along0=min(alongs),
        along1=max(alongs),
        across=min(acrosses),
    )


def _angle_bucket(angle: float) -> int:
    """Group spans whose angles agree to within the spec's 2 degrees."""
    return int(round(angle / ANGLE_TOLERANCE_DEG))


def merge_page_spans(spans: list[PdfTextSpan] | tuple[PdfTextSpan, ...]) -> list[MergedLine]:
    """Merge glyph runs into logical lines. Deterministic for a given input."""
    if not spans:
        return []

    projected = [_project(s) for s in spans if s.text.strip()]
    if not projected:
        return []

    groups: dict[int, list[_Projected]] = {}
    for p in projected:
        groups.setdefault(_angle_bucket(p.angle), []).append(p)

    lines: list[MergedLine] = []
    for bucket in sorted(groups):
        members = groups[bucket]
        # Reading order within one writing direction: down the page first
        # (pdf_points is y-UP, so descending `across`), then along the line.
        members.sort(key=lambda p: (-round(p.across, 1), p.along0))
        current: list[_Projected] = [members[0]]
        for p in members[1:]:
            prev = current[-1]
            em = prev.em
            same_baseline = abs(p.across - prev.across) <= BASELINE_TOLERANCE_EM * em
            same_angle = abs(p.angle - prev.angle) <= ANGLE_TOLERANCE_DEG
            same_size = abs(p.em - em) <= FONT_SIZE_TOLERANCE * max(p.em, em)
            gap = p.along0 - prev.along1
            if same_baseline and same_angle and same_size and gap <= JOIN_GAP_EM * em:
                current.append(p)
            else:
                lines.append(_finish(current))
                current = [p]
        lines.append(_finish(current))
    return lines


def _finish(members: list[_Projected]) -> MergedLine:
    parts: list[str] = [members[0].span.text.strip()]
    for prev, cur in zip(members, members[1:], strict=False):
        gap = cur.along0 - prev.along1
        if gap > SPACE_GAP_EM * prev.em:
            parts.append(" ")
        parts.append(cur.span.text.strip())
    text = "".join(parts).strip()

    boxes = [m.span.bbox for m in members]
    bbox = BBox(
        min(b.x0 for b in boxes),
        min(b.y0 for b in boxes),
        max(b.x1 for b in boxes),
        max(b.y1 for b in boxes),
    )
    first = members[0].span
    return MergedLine(
        text=text,
        normalized_text=normalize_text(text),
        bbox=bbox,
        rotation_deg=round(members[0].angle, 4),
        font_name=first.font_name,
        font_size_pt=first.font_size,
        span_count=len(members),
        block_index=first.block_index,
    )


# --- normalisation --------------------------------------------------------
#
# Per the TextSpan docstring and §2.2: uppercase, collapse whitespace, expand
# unicode fractions, canonicalise dimensional fractions ('3/4"' -> '0.75IN',
# '1-1/2"' -> '1.5IN'). Matching always uses normalized_text; display always
# uses text.

#: Column width of ``text_span.normalized_text``.
NORMALIZED_MAX_LEN = 512

_FRACTION_INCH = re.compile(r"(?<![\d/])(?:(\d+)[\s\-])?(\d+)/(\d+)\s*(?:\"|”|IN\b|INCH\b)")
# Whole inches only in quote form, and never straight after a feet mark or a
# hyphen: 24'-6" is a feet-and-inches dimension, not a 6 inch size, and
# mangling it into 24'-6IN would break the dimension parser in stage C.
_WHOLE_INCH = re.compile(r"(?<![\d/.\-'’])(\d+(?:\.\d+)?)\s*(?:\"|”)")
_WS = re.compile(r"\s+")


def _expand_unicode_fractions(text: str) -> str:
    out: list[str] = []
    for ch in text:
        decomposed = unicodedata.decomposition(ch)
        if decomposed.startswith("<fraction>"):
            # e.g. '¾' -> '<fraction> 0033 2044 0034' -> '3/4'
            parts = decomposed.split()[1:]
            out.append("".join(chr(int(code, 16)) for code in parts).replace("\u2044", "/"))
        else:
            out.append(ch)
    return "".join(out)


def _format_inches(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{text or '0'}IN"


def normalize_text(text: str) -> str:
    """Canonical form used for every match. Never shown to a user."""
    s = _expand_unicode_fractions(text)
    s = _WS.sub(" ", s).strip().upper()

    def _frac(match: re.Match[str]) -> str:
        whole = float(match.group(1) or 0)
        num, den = float(match.group(2)), float(match.group(3))
        if den == 0:
            return match.group(0)
        return _format_inches(whole + num / den)

    s = _FRACTION_INCH.sub(_frac, s)
    s = _WHOLE_INCH.sub(lambda m: _format_inches(float(m.group(1))), s)
    return s[:NORMALIZED_MAX_LEN]
