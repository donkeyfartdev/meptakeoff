"""Stage B end to end over the ingested corpus, plus the abstain path.

What matters here is not "did it get E-101 right" — the corpus is synthetic and
no accuracy claim may come from it (``AGENTS.md`` §6). What matters is that the
*behaviour* holds: a class is only written when a rule fired and can be named,
a sheet whose evidence is missing is skipped rather than guessed at, and every
classification leaves an explainable trail.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from conduit.classify import ABSTAIN_CONFIDENCE, decide
from conduit.classify.regions import ScoredSpan
from conduit.db.models import (
    AuditEvent,
    ClassificationMethod,
    Discipline,
    PageTaskState,
    Sheet,
    SheetSubtype,
    StageName,
    TaskStatus,
)
from conduit.geometry import BBox, PageGeometry


def _sheets(env) -> dict[int, Sheet]:
    session = env.Session()
    try:
        rows = session.execute(
            select(Sheet).where(Sheet.document_id == env.result.document_id)
        ).scalars().all()
        for row in rows:
            session.expunge(row)
        return {row.page_number: row for row in rows}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def test_every_sheet_with_text_gets_a_named_rule_not_a_guess(classified) -> None:
    corrupt = set(classified.manifest.corrupt_pages)
    raster = set(classified.manifest.raster_pages)
    sheets = _sheets(classified)
    for spec in classified.manifest.pages:
        page = spec["page_number"]
        if page in corrupt or page in raster:
            continue
        sheet = sheets[page]
        assert sheet.sheet_number == spec["sheet_number"], (
            f"page {page}: read {sheet.sheet_number!r} from the title block"
        )
        assert sheet.discipline is not Discipline.UNKNOWN
        assert sheet.classification_method is ClassificationMethod.SHEET_NUMBER_REGEX
        assert sheet.classification_confidence >= ABSTAIN_CONFIDENCE
        assert sheet.classification_run_id == classified.result.run_id
        assert sheet.sheet_title and len(sheet.sheet_title) >= 8


def test_rotated_pages_are_classified_like_any_other(classified) -> None:
    """A ``/Rotate 90`` page and a sideways title block must both read."""
    sheets = _sheets(classified)
    rotated = [
        s for s in sheets.values() if s.rotation_deg in (90, 180, 270) and s.sheet_number
    ]
    assert rotated, "the corpus must contain classified rotated pages"
    assert all(s.discipline is not Discipline.UNKNOWN for s in rotated)


def test_pages_that_failed_stage_a_are_skipped_not_guessed(classified) -> None:
    corrupt = set(classified.manifest.corrupt_pages)
    sheets = _sheets(classified)
    for page in corrupt:
        sheet = sheets[page]
        assert sheet.discipline is Discipline.UNKNOWN
        assert sheet.subtype is SheetSubtype.UNKNOWN
        assert sheet.classification_confidence == 0.0
        assert sheet.classification_run_id is None, (
            "a skipped sheet must not claim to have been classified by this run"
        )

    session = classified.Session()
    try:
        rows = session.execute(
            select(PageTaskState.page_number, PageTaskState.status, PageTaskState.error).where(
                PageTaskState.pipeline_run_id == classified.result.run_id,
                PageTaskState.stage == StageName.CLASSIFY,
            )
        ).all()
    finally:
        session.close()
    by_page = {page: (status, error) for page, status, error in rows}
    assert set(by_page) == set(range(1, classified.result.page_count + 1)), (
        "every page needs a classify task row, including the ones that were skipped"
    )
    for page in corrupt:
        status, error = by_page[page]
        assert status is TaskStatus.SKIPPED
        assert "stage A failed" in (error or "")
    assert sum(1 for status, _ in by_page.values() if status is TaskStatus.DONE) == (
        classified.result.page_count - len(corrupt)
    )


def test_a_page_with_no_vector_text_abstains(classified) -> None:
    """The flattened-raster page is the OCR case; week 1 records and skips it."""
    sheets = _sheets(classified)
    for page in classified.manifest.raster_pages:
        sheet = sheets[page]
        assert sheet.discipline is Discipline.UNKNOWN
        assert sheet.classification_confidence < ABSTAIN_CONFIDENCE
        assert sheet.classification_method is ClassificationMethod.DEFAULT_FALLBACK


def test_classification_events_carry_the_rationale(classified) -> None:
    session = classified.Session()
    try:
        payloads = session.execute(
            select(AuditEvent.payload).where(
                AuditEvent.pipeline_run_id == classified.result.run_id,
                AuditEvent.event_type == "sheet.classified",
            )
        ).scalars().all()
    finally:
        session.close()

    assert payloads
    for payload in payloads:
        rationale = payload["rationale"]
        assert rationale["code_version"] and rationale["rules_version"]
        assert "downstream_policy" in rationale
        assert payload["downstream_policy"] == rationale["downstream_policy"]
        if payload["sheet_number"]:
            # §1.4's example shape: which rule, what it matched, the title vote.
            assert rationale["rule"].startswith("^")
            assert rationale["matched"]
            assert rationale["title_vote"]
            assert "agree" in rationale
            # Provenance: the span and the box the number was read from.
            assert rationale["sheet_number_span_id"]
            bbox = rationale["sheet_number_bbox_pdf_points"]
            assert len(bbox) == 4 and bbox[2] > bbox[0]
            assert rationale["region"] in {
                "right_strip",
                "bottom_band",
                "right_narrow",
                "page_fallback",
            }


def test_every_sheet_gets_exactly_one_terminal_classify_event(classified) -> None:
    session = classified.Session()
    try:
        rows = session.execute(
            select(AuditEvent.event_type, func.count())
            .where(AuditEvent.pipeline_run_id == classified.result.run_id)
            .group_by(AuditEvent.event_type)
        ).all()
    finally:
        session.close()
    counts = dict(rows)
    pages = classified.result.page_count
    corrupt = len(classified.manifest.corrupt_pages)
    assert counts.get("sheet.classified", 0) == pages - corrupt
    assert counts.get("sheet.classify_skipped", 0) == corrupt
    assert counts.get("sheet.classify_failed", 0) == 0
    assert counts.get("run.classified", 0) == 1


def test_the_run_records_the_rules_version_it_used(classified) -> None:
    from conduit.classify import CLASSIFY_VERSION
    from conduit.db.models import PipelineRun

    session = classified.Session()
    try:
        run = session.get(PipelineRun, classified.result.run_id)
        assert run.model_versions["classify"] == CLASSIFY_VERSION
        assert StageName.CLASSIFY.value in run.stages_executed
    finally:
        session.close()


def test_abstain_rate_is_measured_and_attributable(classified) -> None:
    result = classified.classify
    session = classified.Session()
    try:
        abstained_sql = session.execute(
            select(func.count())
            .select_from(Sheet)
            .where(
                Sheet.document_id == classified.result.document_id,
                (Sheet.classification_confidence < ABSTAIN_CONFIDENCE)
                | (Sheet.discipline == Discipline.UNKNOWN),
            )
        ).scalar_one()
    finally:
        session.close()
    # The harness's SQL definition and the in-memory one must agree, or the
    # number in RESULTS.md means something different from the number in the DB.
    assert int(abstained_sql) == len(result.abstained)
    assert result.abstain_rate == pytest.approx(
        len(result.abstained) / classified.result.page_count
    )
    reasons = result.abstain_reasons()
    assert sum(reasons.values()) == len(result.abstained)
    assert set(reasons) <= {
        "page_failed_in_stage_a",
        "no_vector_text",
        "unknown_discipline_with_text",
        "low_confidence_with_text",
        "classification_failed",
    }


# ---------------------------------------------------------------------------
# The decision function, without a database
# ---------------------------------------------------------------------------


def _geometry(rotation: int = 0) -> PageGeometry:
    return PageGeometry(
        page_number=1,
        media_box_x0=0.0,
        media_box_y0=0.0,
        media_box_x1=2592.0,
        media_box_y1=1728.0,
        rotation_deg=rotation,
        render_dpi=72,
    )


def _title_block_spans(number: str, title: str) -> list[ScoredSpan]:
    """A plausible bottom-right title block: six small spans plus the fields."""
    spans = [
        ScoredSpan(
            id=f"filler-{i}",
            text=f"REVISION {i} - 2026-01-0{i}",
            normalized_text=f"REVISION {i} - 2026-01-0{i}",
            bbox=BBox(2300.0, 60.0 + 12 * i, 2560.0, 70.0 + 12 * i),
            font_size_pt=7.0,
        )
        for i in range(1, 6)
    ]
    spans.append(
        ScoredSpan(
            id="title",
            text=title,
            normalized_text=title.upper(),
            bbox=BBox(2300.0, 140.0, 2560.0, 152.0),
            font_size_pt=10.0,
        )
    )
    spans.append(
        ScoredSpan(
            id="number",
            text=f"SHEET NUMBER:  {number}",
            normalized_text=f"SHEET NUMBER: {number}",
            bbox=BBox(2300.0, 30.0, 2560.0, 46.0),
            font_size_pt=12.0,
        )
    )
    return spans


def test_decide_reads_a_title_block() -> None:
    decision = decide(
        geometry=_geometry(),
        spans=_title_block_spans("E-101", "ELECTRICAL - LIGHTING PLAN - LEVEL 1"),
    )
    assert decision.sheet_number == "E-101"
    assert decision.discipline is Discipline.ELECTRICAL
    assert decision.subtype is SheetSubtype.E_LIGHTING
    assert decision.method is ClassificationMethod.SHEET_NUMBER_REGEX
    assert decision.confidence == pytest.approx(0.95)
    assert not decision.abstained
    assert decision.downstream_policy == "subtype_class_list"


def test_decide_abstains_on_an_unmapped_prefix() -> None:
    decision = decide(
        geometry=_geometry(),
        spans=_title_block_spans("ZZ-101", "SOMETHING NOBODY BIDS ON"),
    )
    assert decision.discipline is Discipline.UNKNOWN
    assert decision.subtype is SheetSubtype.UNKNOWN
    assert decision.abstained
    assert decision.downstream_policy == "detection_skipped_generic_linework"


def test_decide_keeps_the_prefix_when_the_title_disagrees() -> None:
    """§1.6: never let the title override the number, but pay for the conflict."""
    decision = decide(
        geometry=_geometry(),
        spans=_title_block_spans("P-201", "ELECTRICAL - LIGHTING PLAN"),
    )
    assert decision.discipline is Discipline.PLUMBING
    assert decision.rationale["agree"] is False
    assert decision.confidence < 0.95


def test_decide_abstains_when_there_is_no_text_at_all() -> None:
    decision = decide(geometry=_geometry(), spans=[])
    assert decision.method is ClassificationMethod.DEFAULT_FALLBACK
    assert decision.discipline is Discipline.UNKNOWN
    assert decision.abstained
    assert decision.rationale["rule"] == "no_text_spans"


def test_a_number_found_outside_every_candidate_region_is_capped() -> None:
    """A number in the middle of the drawing is a lead, not a reading."""
    stray = ScoredSpan(
        id="stray",
        text="M-101",
        normalized_text="M-101",
        bbox=BBox(600.0, 900.0, 660.0, 912.0),
        font_size_pt=9.0,
    )
    decision = decide(geometry=_geometry(), spans=[stray])
    assert decision.sheet_number == "M-101"
    assert decision.confidence <= 0.55
    assert decision.abstained
    assert decision.rationale["region"] == "page_fallback"


def test_the_project_name_is_not_read_as_a_sheet_title() -> None:
    spans = _title_block_spans("E-101", "ELECTRICAL - LIGHTING PLAN - LEVEL 1")
    spans.append(
        ScoredSpan(
            id="project",
            text="A VERY LONG PROJECT NAME THAT REPEATS ON EVERY SINGLE SHEET",
            normalized_text="A VERY LONG PROJECT NAME THAT REPEATS ON EVERY SINGLE SHEET",
            bbox=BBox(2300.0, 160.0, 2560.0, 172.0),
            font_size_pt=8.0,
        )
    )
    repeated = frozenset({"A VERY LONG PROJECT NAME THAT REPEATS ON EVERY SINGLE SHEET"})
    unfiltered = decide(geometry=_geometry(), spans=spans)
    filtered = decide(geometry=_geometry(), spans=spans, repeated_texts=repeated)
    assert "PROJECT NAME" in (unfiltered.sheet_title or "")
    assert filtered.sheet_title == "ELECTRICAL - LIGHTING PLAN - LEVEL 1"
