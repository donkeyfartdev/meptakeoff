"""Stage B — sheet classification (``03-pipeline-specs.md`` §1).

    Sheet + its TextSpan rows + its cached vector paths
        -> title-block region (§1.2)
        -> sheet number, sheet title (§1.2)
        -> discipline, subtype (§1.3)
        -> confidence (§1.7) and the §1.8 downstream policy
        -> Sheet columns + AuditEvent(sheet.classified) with the rationale

The rules this module is built to keep
--------------------------------------
1. **Abstain rather than guess.** A wrong sheet class propagates into every
   downstream quantity, and there is no graceful fallback from a wrong number
   in a bid (risk R1). An unmapped prefix is ``Discipline.UNKNOWN``, a title
   that votes for nothing costs confidence, and §1.8's behaviour at low
   confidence is recorded per sheet as ``downstream_policy`` so D and E cannot
   later pretend they were not told.
2. **Explainable, not just scored.** Every classified sheet writes a
   ``sheet.classified`` audit event carrying the rule that fired, what it
   matched, which region it read, the span ids and bboxes the number and title
   came from, and the region scores that lost. A bare float is not provenance.
3. **Failures are visible.** A sheet whose stage A tasks failed is *not*
   classified — it gets a ``SKIPPED`` ``PageTaskState`` and a
   ``sheet.classify_skipped`` event. A sheet whose classification itself
   raises gets ``FAILED`` plus ``sheet.classify_failed`` with the traceback on
   the task row. Reading the audit trail alone must tell a reviewer which
   sheets have no defensible class.
4. **Evidence is not mutated.** ``TextSpan.role`` stays ``UNKNOWN`` here even
   though stage B knows which span was the sheet number: spans are evidence,
   roles are stage C's interpretation (§1.1, §2). What stage B knows is
   recorded as an interpretation — on ``Sheet`` and in the audit payload —
   never by editing the evidence row it read.

Idempotence: re-running stage B for a run overwrites the same ``Sheet``
columns and appends a new audit event. ``Sheet`` is a container (see its ORM
docstring), so this is an update in place; the history stays recoverable from
``audit_event``.
"""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conduit.classify.regions import (
    RegionScore,
    ScoredSpan,
    candidate_regions,
    score_region,
    spans_inside,
)
from conduit.classify.rules import (
    FORCED_SUBTYPE_BY_PREFIX,
    SheetNumber,
    classification_confidence,
    discipline_for_prefix,
    parse_sheet_number,
    sheet_number_candidates,
    subtype_for_title,
    title_discipline_votes,
)
from conduit.db.models import (
    ActorType,
    AuditEvent,
    ClassificationMethod,
    Discipline,
    Document,
    PageTaskState,
    PipelineRun,
    Sheet,
    SheetSubtype,
    StageName,
    TaskStatus,
    TextSpan,
    utcnow,
)
from conduit.db.session import next_audit_seq
from conduit.geometry import BBox, PageGeometry, Point
from conduit.ingest.metrics import StageTiming, process_peak_rss_mb, stage_timer
from conduit.ingest.paths_dump import decode_paths_blob
from conduit.store.base import ObjectStore

__all__ = [
    "ABSTAIN_CONFIDENCE",
    "CLASSIFY_VERSION",
    "CODE_VERSION",
    "ClassifyResult",
    "Decision",
    "SheetOutcome",
    "classify_document",
    "decide",
]

#: Version of this stage's code, recorded on the run and on every audit row.
CODE_VERSION = "stage-b/1"
#: Version of the rule set, recorded in ``PipelineRun.model_versions``.
CLASSIFY_VERSION = "classify-rules-1"

#: §1.8 / §0 band edge. Below this a sheet has abstained.
ABSTAIN_CONFIDENCE = 0.60
#: §0 high band.
HIGH_CONFIDENCE = 0.80

#: §1.2 — a candidate title is at least this long and this alphabetic.
MIN_TITLE_CHARS = 8
MIN_TITLE_LETTER_FRACTION = 0.60
#: §1.2 — the project name repeats on effectively every sheet.
PROJECT_NAME_PAGE_FRACTION = 0.80
#: Below this many text-bearing pages the "repeats on >80% of pages" test has
#: no signal, so it is not applied at all rather than applied badly.
MIN_PAGES_FOR_REPEAT_FILTER = 3
#: §1.6 — two title blocks within this score are a tie.
TIE_SCORE_DELTA = 0.10
TWO_BLOCK_PENALTY = 0.15
#: §1.6 — a classification made without a title-block region is capped here.
NO_TITLE_BLOCK_CAP = 0.55
#: §1.6 — content rotated while ``/Rotate`` is 0.
DERIVED_ROTATION_FRACTION = 0.60
DERIVED_ROTATION_TOLERANCE_DEG = 5.0


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """What stage B concluded about one sheet, and why."""

    discipline: Discipline = Discipline.UNKNOWN
    subtype: SheetSubtype = SheetSubtype.UNKNOWN
    confidence: float = 0.0
    method: ClassificationMethod = ClassificationMethod.DEFAULT_FALLBACK
    sheet_number: str | None = None
    sheet_title: str | None = None
    rationale: dict = field(default_factory=dict)

    @property
    def abstained(self) -> bool:
        """The harness's definition, verbatim: low confidence OR no discipline."""
        return self.confidence < ABSTAIN_CONFIDENCE or self.discipline is Discipline.UNKNOWN

    @property
    def downstream_policy(self) -> str:
        """§1.8, as a value D and E will read rather than re-derive."""
        if self.confidence >= HIGH_CONFIDENCE:
            return "subtype_class_list"
        if self.confidence >= ABSTAIN_CONFIDENCE:
            return "discipline_superset_review"
        if self.discipline is Discipline.UNKNOWN:
            return "detection_skipped_generic_linework"
        return "discipline_superset_review_queue"


def _letter_fraction(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for ch in text if ch.isalpha()) / len(text)


def _derived_rotation(spans: Sequence[ScoredSpan]) -> int | None:
    """§1.6: content rotated while ``/Rotate`` says 0.

    Returns 90/270 when at least ``DERIVED_ROTATION_FRACTION`` of the page's
    spans are written sideways. The stored ``Sheet.rotation_deg`` is never
    changed — it describes the PDF, not the drawing.
    """
    if not spans:
        return None
    for target in (90, 270):
        hits = sum(
            1
            for s in spans
            if abs((s.rotation_deg % 360) - target) <= DERIVED_ROTATION_TOLERANCE_DEG
        )
        if hits / len(spans) >= DERIVED_ROTATION_FRACTION:
            return target
    return None


def _region_number_span(region: BBox, spans: Sequence[ScoredSpan]) -> ScoredSpan | None:
    for span in spans_inside(region, spans):
        if _span_sheet_number(span) is not None:
            return span
    return None


def _span_sheet_number(span: ScoredSpan) -> SheetNumber | None:
    for candidate in sheet_number_candidates(span.normalized_text or span.text):
        parsed = parse_sheet_number(candidate)
        if parsed is not None:
            return parsed
    return None


def _pick_region(
    scores: list[RegionScore], spans: Sequence[ScoredSpan]
) -> tuple[RegionScore | None, bool]:
    """§1.6's two-title-block rule. Returns ``(winner, penalise)``.

    One completion of the spec: ``right_strip`` and ``right_narrow`` are nested
    windows onto the *same* block, so a tie between them is not two title
    blocks. The penalty is only applied when the tied regions carry **different
    number-bearing spans** — which is what "a consultant's block plus the
    architect's" actually looks like in the data.
    """
    live = [s for s in scores if s.score > 0.0]
    if not live:
        return None, False
    live.sort(key=lambda s: s.score, reverse=True)
    if len(live) == 1 or (live[0].score - live[1].score) > TIE_SCORE_DELTA:
        return live[0], False
    top = [s for s in live if (live[0].score - s.score) <= TIE_SCORE_DELTA]
    numbered = [(s, _region_number_span(s.bbox, spans)) for s in top]
    with_number = [(s, span) for s, span in numbered if span is not None]
    if len(with_number) <= 1:
        return (with_number[0][0] if with_number else top[0]), False
    if len({span.id for _, span in with_number}) == 1:
        return with_number[0][0], False
    contenders = [s for s, _ in with_number]
    right = [s for s in contenders if s.name.startswith("right")]
    # Two blocks, two different numbers: prefer the right strip and say so in
    # the confidence, because one of them belongs to another consultant.
    return (right[0] if right else contenders[0]), True


def _in_lower_quarter(geometry: PageGeometry, region: BBox, span: ScoredSpan) -> bool:
    """"Lower quarter" is a fact about the *displayed* sheet, so it is decided
    in raster space, where ``/Rotate`` has already been applied."""
    cx, cy = span.center
    center_px = geometry.pdf_to_raster(Point(cx, cy))
    region_px = geometry.pdf_bbox_to_raster(region)
    if region_px.height <= 0:
        return False
    return center_px.y >= region_px.y0 + 0.75 * region_px.height


def _extract_sheet_number(
    geometry: PageGeometry, region: BBox, spans: Sequence[ScoredSpan]
) -> tuple[ScoredSpan, SheetNumber] | None:
    """§1.2: largest font first, preferring the lower quarter of the region."""
    hits: list[tuple[float, bool, ScoredSpan, SheetNumber]] = []
    for span in spans:
        parsed = _span_sheet_number(span)
        if parsed is None:
            continue
        hits.append(
            (
                span.font_size_pt or 0.0,
                _in_lower_quarter(geometry, region, span),
                span,
                parsed,
            )
        )
    if not hits:
        return None
    hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
    _, _, span, parsed = hits[0]
    return span, parsed


def _extract_title(
    spans: Sequence[ScoredSpan], *, exclude: ScoredSpan | None, repeated: frozenset[str]
) -> ScoredSpan | None:
    """§1.2: longest alphabetic span that is not the repeating project name."""
    best: ScoredSpan | None = None
    for span in spans:
        if exclude is not None and span.id == exclude.id:
            continue
        text = (span.text or "").strip()
        if len(text) < MIN_TITLE_CHARS:
            continue
        if _letter_fraction(text) < MIN_TITLE_LETTER_FRACTION:
            continue
        if span.normalized_text in repeated:
            continue
        if best is None or len(text) > len((best.text or "").strip()):
            best = span
    return best


def decide(
    *,
    geometry: PageGeometry,
    spans: Sequence[ScoredSpan],
    paths: Sequence[dict] = (),
    repeated_texts: frozenset[str] = frozenset(),
) -> Decision:
    """Classify one sheet. Pure: no session, no store, no PDF library.

    ``geometry`` is the page as stage A recorded it; ``spans`` are the merged
    ``TextSpan`` rows of that page; ``paths`` is the decoded
    ``conduit.page_paths/1`` dump (used only for the ruling-line term of the
    region score, and optional — a page with no dump simply scores 0 on that
    term, which is recorded in the rationale rather than hidden).
    """
    rationale: dict = {
        "code_version": CODE_VERSION,
        "rules_version": CLASSIFY_VERSION,
        "page_number": geometry.page_number,
        "span_count": len(spans),
        "paths_available": bool(paths),
    }

    if not spans:
        # No vector text: nothing to read. This is the OCR case, which week 1
        # records and skips (roadmap W1 "explicitly NOT"). Abstain loudly.
        rationale["rule"] = "no_text_spans"
        rationale["abstain_reason"] = "page has no vector text (OCR not built)"
        decision = Decision(
            confidence=classification_confidence(
                ClassificationMethod.DEFAULT_FALLBACK,
                title_agrees=False,
                from_index=False,
                subtype_matched=False,
            ),
            rationale=rationale,
        )
        rationale["downstream_policy"] = decision.downstream_policy
        return decision

    derived = _derived_rotation(spans) if geometry.rotation_deg == 0 else None
    if derived is not None:
        rationale["derived_rotation"] = derived
    search_geometry = geometry
    if derived is not None:
        search_geometry = PageGeometry(
            page_number=geometry.page_number,
            media_box_x0=geometry.media_box_x0,
            media_box_y0=geometry.media_box_y0,
            media_box_x1=geometry.media_box_x1,
            media_box_y1=geometry.media_box_y1,
            rotation_deg=derived,
            render_dpi=geometry.render_dpi,
        )

    scores = [
        score_region(name, bbox, spans, paths)
        for name, bbox in candidate_regions(search_geometry)
    ]
    winner, two_blocks = _pick_region(scores, spans)
    rationale["regions"] = [s.as_payload() for s in scores]

    if two_blocks:
        rationale["two_title_blocks"] = True

    # The number is looked for in the winning region first, then in the other
    # scoring candidates, and only then across the whole page. The last step is
    # §1.6's "unnumbered sheet" branch with no drawing index to fall back on
    # (stage C is not built, so no index exists yet): whatever it finds is
    # capped below the abstain line, because a number found outside every
    # title-block candidate is a lead, not a reading.
    cap = 1.0
    search: list[tuple[str, BBox]] = []
    if winner is not None:
        search.append((winner.name, winner.bbox))
        search += [
            (s.name, s.bbox) for s in sorted(scores, key=lambda s: s.score, reverse=True)
            if s.score > 0.0 and s.name != winner.name
        ]
    region_name = winner.name if winner is not None else "page_fallback"
    region = winner.bbox if winner is not None else geometry.media_box()
    number_span: ScoredSpan | None = None
    number: SheetNumber | None = None
    for name, bbox in search:
        found = _extract_sheet_number(search_geometry, bbox, spans_inside(bbox, spans))
        if found is not None:
            region_name, region = name, bbox
            number_span, number = found
            break
    if number is None:
        whole = geometry.media_box()
        found = _extract_sheet_number(search_geometry, whole, spans)
        if found is not None:
            number_span, number = found
            region_name, region = "page_fallback", whole
            cap = NO_TITLE_BLOCK_CAP
            rationale["region_fallback_reason"] = (
                "no candidate title-block region contained a sheet number"
            )
        elif winner is None:
            region_name, region = "page_fallback", whole
            cap = NO_TITLE_BLOCK_CAP
            rationale["region_fallback_reason"] = "no candidate region scored above zero"

    rationale["region"] = region_name
    if winner is not None:
        rationale["top_region"] = winner.name
        rationale["region_score"] = round(winner.score, 4)
    region_spans = spans_inside(region, spans)
    title_span = _extract_title(region_spans, exclude=number_span, repeated=repeated_texts)
    title = (title_span.text or "").strip() if title_span is not None else None

    if number is not None:
        discipline = discipline_for_prefix(number.prefix)
        method = ClassificationMethod.SHEET_NUMBER_REGEX
        rationale["rule"] = f"^{number.prefix}-"
        rationale["matched"] = number.raw
        rationale["sheet_number_span_id"] = str(number_span.id) if number_span else None
        rationale["sheet_number_bbox_pdf_points"] = (
            [round(v, 2) for v in number_span.bbox.as_tuple()] if number_span else None
        )
    else:
        discipline = Discipline.UNKNOWN
        method = (
            ClassificationMethod.TITLE_BLOCK_KEYWORDS
            if title
            else ClassificationMethod.DEFAULT_FALLBACK
        )
        rationale["rule"] = "title_keywords_only" if title else "no_sheet_number_no_title"
        cap = min(cap, NO_TITLE_BLOCK_CAP)

    forced = FORCED_SUBTYPE_BY_PREFIX.get(number.prefix.upper()) if number else None
    if forced is not None:
        subtype, matched_keyword = forced, f"prefix:{number.prefix}"
    else:
        subtype, matched_keyword = subtype_for_title(discipline, title)

    votes = title_discipline_votes(title)
    title_agrees = bool(votes) and discipline in votes
    confidence = classification_confidence(
        method,
        title_agrees=title_agrees,
        from_index=False,
        subtype_matched=subtype is not SheetSubtype.OTHER,
    )
    if two_blocks:
        confidence = round(max(confidence - TWO_BLOCK_PENALTY, 0.0), 4)
    confidence = round(min(confidence, cap), 4)

    if title_span is not None:
        rationale["sheet_title_span_id"] = str(title_span.id)
        rationale["sheet_title_bbox_pdf_points"] = [
            round(v, 2) for v in title_span.bbox.as_tuple()
        ]
    rationale["title_vote"] = subtype.value
    rationale["title_keyword"] = matched_keyword
    rationale["title_discipline_votes"] = sorted(d.value for d in votes)
    rationale["agree"] = title_agrees
    rationale["confidence_inputs"] = {
        "method": method.value,
        "title_agrees": title_agrees,
        "from_index": False,
        "subtype_matched": subtype is not SheetSubtype.OTHER,
        "two_title_block_penalty": TWO_BLOCK_PENALTY if two_blocks else 0.0,
        "cap": cap,
    }

    decision = Decision(
        discipline=discipline,
        subtype=subtype if discipline is not Discipline.UNKNOWN else SheetSubtype.UNKNOWN,
        confidence=confidence,
        method=method,
        sheet_number=number.normalised if number else None,
        sheet_title=title,
        rationale=rationale,
    )
    rationale["downstream_policy"] = decision.downstream_policy
    if decision.abstained:
        rationale["abstained"] = True
    return decision


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class SheetOutcome:
    page_number: int
    sheet_id: uuid.UUID
    status: str
    duration_ms: int = 0
    peak_rss_mb: int = 0
    decision: Decision | None = None
    error: str | None = None

    @property
    def abstained(self) -> bool:
        """A skipped or failed sheet has no defensible class, so it abstains."""
        return self.decision is None or self.decision.abstained

    @property
    def has_text(self) -> bool:
        return bool(self.decision and self.decision.rationale.get("span_count"))

    @property
    def abstain_reason(self) -> str | None:
        """*Why* the sheet abstained — the harness's decision needs this.

        An abstain rate is not actionable on its own: "the page had no text
        layer at all" and "the text was there and the rules could not read it"
        point at completely different work (OCR vs a better classifier).
        """
        if not self.abstained:
            return None
        if self.status == TaskStatus.SKIPPED.value:
            return "page_failed_in_stage_a"
        if self.status == TaskStatus.FAILED.value:
            return "classification_failed"
        if self.decision is None or self.decision.rationale.get("rule") == "no_text_spans":
            return "no_vector_text"
        if self.decision.discipline is Discipline.UNKNOWN:
            return "unknown_discipline_with_text"
        return "low_confidence_with_text"


@dataclass
class ClassifyResult:
    run_id: uuid.UUID
    document_id: uuid.UUID
    sheets: list[SheetOutcome] = field(default_factory=list)
    peak_rss_mb: int = 0

    @property
    def classified(self) -> list[SheetOutcome]:
        return [s for s in self.sheets if s.status == TaskStatus.DONE.value]

    @property
    def skipped(self) -> list[SheetOutcome]:
        return [s for s in self.sheets if s.status == TaskStatus.SKIPPED.value]

    @property
    def failed(self) -> list[SheetOutcome]:
        return [s for s in self.sheets if s.status == TaskStatus.FAILED.value]

    @property
    def abstained(self) -> list[SheetOutcome]:
        return [s for s in self.sheets if s.abstained]

    @property
    def abstain_rate(self) -> float:
        """`confidence < 0.60 OR discipline = UNKNOWN`, over every sheet."""
        return (len(self.abstained) / len(self.sheets)) if self.sheets else 0.0

    @property
    def with_text(self) -> list[SheetOutcome]:
        return [s for s in self.sheets if s.has_text]

    @property
    def abstain_rate_with_text(self) -> float:
        """Abstain rate over sheets that actually had a text layer to read.

        The headline rate mixes two different failures. This one isolates the
        rules' own behaviour; both are reported, neither replaces the other.
        """
        pool = self.with_text
        if not pool:
            return 0.0
        return sum(1 for s in pool if s.abstained) / len(pool)

    def abstain_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sheet in self.abstained:
            key = sheet.abstain_reason or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def method_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sheet in self.sheets:
            key = sheet.decision.method.value if sheet.decision else sheet.status
            counts[key] = counts.get(key, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _audit(
    session: Session,
    *,
    event_type: str,
    entity_id: uuid.UUID | None,
    document_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
    payload: dict,
) -> None:
    session.add(
        AuditEvent(
            seq=next_audit_seq(session),
            occurred_at=utcnow(),
            document_id=document_id,
            pipeline_run_id=pipeline_run_id,
            entity_type="sheet" if entity_id is not None else "pipeline_run",
            entity_id=entity_id,
            event_type=event_type,
            actor_type=ActorType.SYSTEM,
            actor_id=CODE_VERSION,
            payload=payload,
        )
    )


def _record_task(
    session: Session,
    *,
    run: PipelineRun,
    sheet: Sheet,
    status: TaskStatus,
    timing: StageTiming,
    error: str | None,
) -> None:
    task = session.execute(
        select(PageTaskState).where(
            PageTaskState.pipeline_run_id == run.id,
            PageTaskState.page_number == sheet.page_number,
            PageTaskState.stage == StageName.CLASSIFY,
        )
    ).scalar_one_or_none()
    if task is None:
        task = PageTaskState(
            pipeline_run_id=run.id, page_number=sheet.page_number, stage=StageName.CLASSIFY
        )
        session.add(task)
    task.sheet_id = sheet.id
    task.status = status
    task.attempts = (task.attempts or 0) + 1
    finished: datetime = utcnow()
    task.started_at = finished - timedelta(milliseconds=timing.duration_ms)
    task.finished_at = finished
    task.duration_ms = timing.duration_ms
    task.peak_rss_mb = timing.peak_rss_mb
    task.error = error
    session.flush()


def _sheet_geometry(sheet: Sheet) -> PageGeometry:
    return PageGeometry(
        page_number=sheet.page_number,
        media_box_x0=sheet.media_box_x0,
        media_box_y0=sheet.media_box_y0,
        media_box_x1=sheet.media_box_x1,
        media_box_y1=sheet.media_box_y1,
        rotation_deg=sheet.rotation_deg,
        render_dpi=sheet.render_dpi,
    )


def _load_spans(session: Session, *, run: PipelineRun, sheet: Sheet) -> list[ScoredSpan]:
    rows = session.execute(
        select(TextSpan)
        .where(TextSpan.pipeline_run_id == run.id, TextSpan.sheet_id == sheet.id)
        .order_by(TextSpan.id)
    ).scalars()
    return [
        ScoredSpan(
            id=row.id,
            text=row.text,
            normalized_text=row.normalized_text,
            bbox=BBox(row.bbox_x0, row.bbox_y0, row.bbox_x1, row.bbox_y1),
            font_size_pt=row.font_size_pt,
            rotation_deg=row.rotation_deg or 0.0,
        )
        for row in rows
    ]


def _repeated_texts(session: Session, *, run: PipelineRun) -> frozenset[str]:
    """Normalised strings that repeat on > 80% of the run's text-bearing pages.

    §1.2 says "> 80% of pages". The denominator here is pages that *have* text,
    not every page: a flattened-raster page cannot carry the project name, and
    counting it dilutes the signal until the project name survives the filter
    and gets read as a sheet title. Deliberate, and the reason is this
    sentence.
    """
    pages_with_text = session.execute(
        select(func.count(func.distinct(TextSpan.sheet_id))).where(
            TextSpan.pipeline_run_id == run.id
        )
    ).scalar_one()
    if not pages_with_text or pages_with_text < MIN_PAGES_FOR_REPEAT_FILTER:
        return frozenset()
    rows = session.execute(
        select(TextSpan.normalized_text, func.count(func.distinct(TextSpan.sheet_id)))
        .where(TextSpan.pipeline_run_id == run.id)
        .group_by(TextSpan.normalized_text)
    ).all()
    threshold = PROJECT_NAME_PAGE_FRACTION * pages_with_text
    return frozenset(text for text, pages in rows if pages > threshold)


def _load_paths(store: ObjectStore | None, sheet: Sheet) -> list[dict]:
    """Read the page's cached vector paths through ``Sheet.paths_object_key``.

    This is the column added alongside this stage: before it existed the key
    was only reachable by parsing a JSON audit payload. A missing key or a
    store that has lost the object is not an error — the ruling-line term of
    the region score is simply unavailable, and ``paths_available`` in the
    rationale says so.
    """
    if store is None or not sheet.paths_object_key:
        return []
    try:
        blob = store.get_bytes(sheet.paths_object_key)
    except Exception:  # noqa: BLE001 - absence is a fact, not a failure
        return []
    payload = decode_paths_blob(blob)
    paths = payload.get("paths", [])
    return paths if isinstance(paths, list) else []


def classify_document(
    *,
    session: Session,
    document: Document,
    run: PipelineRun,
    store: ObjectStore | None = None,
) -> ClassifyResult:
    """Run stage B over every sheet of ``document`` within ``run``."""
    result = ClassifyResult(run_id=run.id, document_id=document.id)
    repeated = _repeated_texts(session, run=run)

    run.model_versions = {**(run.model_versions or {}), "classify": CLASSIFY_VERSION}
    if StageName.CLASSIFY.value not in (run.stages_executed or []):
        run.stages_executed = [*(run.stages_executed or []), StageName.CLASSIFY.value]
    session.flush()

    sheets = session.execute(
        select(Sheet).where(Sheet.document_id == document.id).order_by(Sheet.page_number)
    ).scalars().all()

    for sheet in sheets:
        outcome = _classify_one(
            session=session,
            run=run,
            document=document,
            sheet=sheet,
            store=store,
            repeated=repeated,
        )
        result.sheets.append(outcome)
        session.commit()

    result.peak_rss_mb = process_peak_rss_mb()
    _audit(
        session,
        event_type="run.classified",
        entity_id=None,
        document_id=document.id,
        pipeline_run_id=run.id,
        payload={
            "sheets": len(result.sheets),
            "classified": len(result.classified),
            "skipped": len(result.skipped),
            "failed": len(result.failed),
            "abstained": len(result.abstained),
            "abstain_rate": round(result.abstain_rate, 4),
            "abstain_rate_sheets_with_text": round(result.abstain_rate_with_text, 4),
            "abstain_reasons": result.abstain_reasons(),
            "abstain_definition": "classification_confidence < 0.60 OR discipline = UNKNOWN",
            "methods": result.method_counts(),
            "rules_version": CLASSIFY_VERSION,
        },
    )
    session.commit()
    return result


def _failed_stage_a_stages(session: Session, *, run: PipelineRun, sheet: Sheet) -> list[str]:
    rows = session.execute(
        select(PageTaskState.stage).where(
            PageTaskState.pipeline_run_id == run.id,
            PageTaskState.page_number == sheet.page_number,
            PageTaskState.stage != StageName.CLASSIFY,
            PageTaskState.status == TaskStatus.FAILED,
        )
    ).scalars().all()
    return [s.value for s in rows]


def _classify_one(
    *,
    session: Session,
    run: PipelineRun,
    document: Document,
    sheet: Sheet,
    store: ObjectStore | None,
    repeated: frozenset[str],
) -> SheetOutcome:
    blocked = _failed_stage_a_stages(session, run=run, sheet=sheet)
    if blocked:
        # Stage A did not produce this page's evidence. Classifying it anyway
        # would mean publishing a class derived from nothing.
        with stage_timer() as timing:
            pass
        reason = f"skipped: stage A failed for this page ({', '.join(sorted(blocked))})"
        _record_task(
            session, run=run, sheet=sheet, status=TaskStatus.SKIPPED, timing=timing, error=reason
        )
        _audit(
            session,
            event_type="sheet.classify_skipped",
            entity_id=sheet.id,
            document_id=document.id,
            pipeline_run_id=run.id,
            payload={
                "page_number": sheet.page_number,
                "reason": reason,
                "failed_stage_a_stages": sorted(blocked),
                "traceback_location": (
                    f"page_task_state(pipeline_run_id={run.id}, page_number={sheet.page_number})"
                ),
            },
        )
        return SheetOutcome(
            page_number=sheet.page_number,
            sheet_id=sheet.id,
            status=TaskStatus.SKIPPED.value,
            duration_ms=timing.duration_ms,
            peak_rss_mb=timing.peak_rss_mb,
            error=reason,
        )

    decision: Decision | None = None
    error: str | None = None
    status = TaskStatus.DONE
    with stage_timer() as timing:
        try:
            spans = _load_spans(session, run=run, sheet=sheet)
            paths = _load_paths(store, sheet)
            decision = decide(
                geometry=_sheet_geometry(sheet),
                spans=spans,
                paths=paths,
                repeated_texts=repeated,
            )
            del paths
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            status = TaskStatus.FAILED
            error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    if status is TaskStatus.FAILED:
        _record_task(
            session, run=run, sheet=sheet, status=status, timing=timing, error=error
        )
        _audit(
            session,
            event_type="sheet.classify_failed",
            entity_id=sheet.id,
            document_id=document.id,
            pipeline_run_id=run.id,
            payload={
                "page_number": sheet.page_number,
                "error": (error or "").strip().splitlines()[-1:],
                "traceback_location": (
                    f"page_task_state(pipeline_run_id={run.id}, page_number={sheet.page_number})"
                ),
            },
        )
        return SheetOutcome(
            page_number=sheet.page_number,
            sheet_id=sheet.id,
            status=status.value,
            duration_ms=timing.duration_ms,
            peak_rss_mb=timing.peak_rss_mb,
            error=error,
        )

    assert decision is not None
    sheet.sheet_number = decision.sheet_number[:64] if decision.sheet_number else None
    sheet.sheet_title = decision.sheet_title[:512] if decision.sheet_title else None
    sheet.discipline = decision.discipline
    sheet.subtype = decision.subtype
    sheet.classification_confidence = decision.confidence
    sheet.classification_method = decision.method
    sheet.classification_run_id = run.id
    session.flush()

    _record_task(session, run=run, sheet=sheet, status=status, timing=timing, error=None)
    _audit(
        session,
        event_type="sheet.classified",
        entity_id=sheet.id,
        document_id=document.id,
        pipeline_run_id=run.id,
        payload={
            "page_number": sheet.page_number,
            "sheet_number": decision.sheet_number,
            "sheet_title": decision.sheet_title,
            "discipline": decision.discipline.value,
            "subtype": decision.subtype.value,
            "confidence": decision.confidence,
            "method": decision.method.value,
            "abstained": decision.abstained,
            "downstream_policy": decision.downstream_policy,
            "rationale": decision.rationale,
        },
    )
    return SheetOutcome(
        page_number=sheet.page_number,
        sheet_id=sheet.id,
        status=status.value,
        duration_ms=timing.duration_ms,
        peak_rss_mb=timing.peak_rss_mb,
        decision=decision,
    )
