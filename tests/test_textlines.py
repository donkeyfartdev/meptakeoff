"""The span->line merge of ``03-pipeline-specs.md`` §2.2, and normalisation.

These are unit tests over constructed spans rather than corpus output, because
the merge rules are stated in the spec as arithmetic and can be checked as
arithmetic — including the rotated case, which the synthetic corpus cannot
exercise cleanly (see ``test_ingest_stage_a.py::
test_rotated_title_block_wraps_in_the_corpus``).
"""

from __future__ import annotations

import pytest

from conduit.geometry import BBox, Point
from conduit.ingest.textlines import merge_page_spans, normalize_text
from conduit.pdf.backend import PdfTextSpan


def span(
    text: str,
    x0: float,
    y0: float,
    *,
    size: float = 10.0,
    direction: Point = Point(1.0, 0.0),
    width: float | None = None,
) -> PdfTextSpan:
    w = width if width is not None else 0.5 * size * len(text)
    if direction.x:
        bbox = BBox(x0, y0, x0 + w, y0 + size)
    else:
        bbox = BBox(x0, y0, x0 + size, y0 + w)
    return PdfTextSpan(
        text=text,
        bbox=bbox,
        font_name="Helv",
        font_size=size,
        block_index=0,
        line_index=0,
        span_index=0,
        direction=direction,
    )


def test_adjacent_runs_on_one_baseline_become_one_line() -> None:
    spans = [span("1", 0, 100, width=5), span("-1/2", 6, 100, width=20), span('"', 27, 100, width=4)]
    (line,) = merge_page_spans(spans)
    assert line.text == '1-1/2"'
    assert line.span_count == 3
    assert line.bbox.x0 == 0 and line.bbox.x1 == pytest.approx(31)


def test_a_wide_gap_starts_a_new_line() -> None:
    spans = [span("LEFT", 0, 100), span("RIGHT", 400, 100)]
    lines = merge_page_spans(spans)
    assert [line.text for line in lines] == ["LEFT", "RIGHT"]


def test_a_medium_gap_inserts_a_space() -> None:
    """gap > 0.6 em joins with a space; gap <= 0.6 em joins with nothing."""
    spaced = merge_page_spans([span("AB", 0, 0, width=10), span("CD", 17, 0, width=10)])
    assert [line.text for line in spaced] == ["AB CD"], "gap 7pt > 0.6 em (6pt)"
    glued = merge_page_spans([span("AB", 0, 0, width=10), span("CD", 15, 0, width=10)])
    assert [line.text for line in glued] == ["ABCD"], "gap 5pt <= 0.6 em (6pt)"


def test_different_baselines_do_not_merge() -> None:
    spans = [span("TOP", 0, 100), span("BOTTOM", 30, 80)]
    lines = merge_page_spans(spans)
    assert sorted(line.text for line in lines) == ["BOTTOM", "TOP"]


def test_different_font_sizes_do_not_merge() -> None:
    """The documented addition to §2.2 — two fields, not one line."""
    spans = [span("SHEET NUMBER:", 0, 0, size=12), span("E-101", 80, 0, size=8)]
    assert len(merge_page_spans(spans)) == 2


def test_rotated_text_merges_along_its_own_direction() -> None:
    up = Point(0.0, 1.0)
    spans = [
        span("PANEL", 500, 100, direction=up),
        span("SCHEDULE", 500, 134, direction=up, width=40),
    ]
    (line,) = merge_page_spans(spans)
    assert line.text == "PANEL SCHEDULE"
    assert line.rotation_deg == pytest.approx(90.0)


def test_horizontal_and_vertical_text_never_merge() -> None:
    spans = [span("ACROSS", 500, 100), span("DOWN", 500, 100, direction=Point(0.0, 1.0))]
    lines = merge_page_spans(spans)
    assert len(lines) == 2
    assert {round(line.rotation_deg) for line in lines} == {0, 90}


def test_empty_and_blank_input() -> None:
    assert merge_page_spans([]) == []
    assert merge_page_spans([span("   ", 0, 0)]) == []


def test_merge_is_deterministic() -> None:
    spans = [span("C", 40, 0), span("A", 0, 0), span("B", 20, 0), span("D", 0, 40)]
    first = [line.text for line in merge_page_spans(spans)]
    second = [line.text for line in merge_page_spans(list(reversed(spans)))]
    assert first == second


# --- normalisation --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('3/4" CONDUIT', "0.75IN CONDUIT"),          # spec example
        ('1-1/2" EMT', "1.5IN EMT"),                 # spec example
        ("\u00be\" C", "0.75IN C"),                  # unicode fraction
        ('2" PIPE', "2IN PIPE"),
        ("  mixed   Case\ttext ", "MIXED CASE TEXT"),
        ("24'-6\"", "24'-6\""),                      # feet-inches left alone
        ('SCALE: 1/8" = 1\'-0"', "SCALE: 0.125IN = 1'-0\""),
        ("3/0 AWG", "3/0 AWG"),                      # not a dimension
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_normalized_text_fits_the_column() -> None:
    assert len(normalize_text("A" * 900)) == 512
