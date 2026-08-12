"""Stage A — ingest, end to end.

    PDF bytes -> object store (sha256 in flight)
              -> Document + PipelineRun + Sheet rows
              -> per page: geometry (no render), tile pyramid, thumbnail,
                 paths.json.zst, merged TextSpan rows
              -> PageTaskState per page per stage, with duration and RSS
              -> run ends completed / completed_with_errors

The rules this module is built to keep
--------------------------------------
1. **Evidence is immutable, interpretation is versioned.** The uploaded PDF is
   stored once, content-addressed, and never rewritten. Every ``TextSpan`` row
   carries the ``pipeline_run_id`` that produced it; a re-run makes new rows
   rather than editing old ones. ``Sheet`` is a container and *is* updated in
   place, which the ORM docstring permits.
2. **No quantity without provenance.** Every row this stage writes names its
   sheet, page number and bounding box in a declared coordinate space, and the
   run that made it names the backend and merge versions that produced it
   (``PipelineRun.model_versions``).
3. **One page raster live per worker** (risk R8) — enforced, not asserted, by
   ``conduit.ingest.render.page_pixels``.
4. **A page failure never fails the run.** Each page/stage task is caught,
   recorded on ``PageTaskState`` with its traceback, and the loop continues;
   the run finishes ``completed_with_errors``. The synthetic corpus contains
   two deliberately corrupt pages precisely so this path is exercised on every
   test run.
5. **A page failure is visible in the audit trail.** Every page emits exactly
   one of ``sheet.ingested`` or ``sheet.ingest_failed`` — never a success event
   for a page that failed. Reading the ``audit_event`` table alone must tell a
   reviewer which pages did not ingest; anything less is an audit trail that
   quietly says everything was fine.

Idempotence
-----------
Content addressing does most of the work: identical bytes produce an identical
sha256, hence an identical object key, hence no second blob. On top of that,
``Document`` is unique per ``(project_id, sha256)``, so re-ingesting the same
file finds the existing row. What happens next is the caller's choice:

* ``if_exists="reuse_run"`` (default) — return the existing completed run at
  the same DPI and do no work at all;
* ``if_exists="new_run"`` — make a new ``PipelineRun`` over the same document
  and re-derive everything. Rasters and tiles dedupe in the store, so the only
  new bytes are the new evidence rows.
"""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conduit.db.models import (
    ActorType,
    AuditEvent,
    CoordinateSpace,
    Document,
    PageTaskState,
    PipelineRun,
    Project,
    ProjectStatus,
    RunStatus,
    Sheet,
    SpanRole,
    StageName,
    TaskStatus,
    TextSource,
    TextSpan,
    utcnow,
)
from conduit.db.session import next_audit_seq
from conduit.errors import ConduitError, PageLevelError
from conduit.geometry import PageGeometry
from conduit.ingest.config import StageAConfig
from conduit.ingest.metrics import StageTiming, process_peak_rss_mb, stage_timer
from conduit.ingest.paths_dump import dump_paths
from conduit.ingest.render import PixmapBudgetExceeded
from conduit.ingest.textlines import MERGE_VERSION, merge_page_spans
from conduit.ingest.tiles import build_pyramid, render_full_page
from conduit.pdf.backend import PdfBackend
from conduit.store.base import ObjectRef, ObjectStore

__all__ = [
    "CODE_VERSION",
    "IngestResult",
    "PageReport",
    "ingest_pdf",
]

#: Version of this stage's code, recorded on every run it creates.
CODE_VERSION = "stage-a/1"

STAGE_A_STAGES = (StageName.RASTERISE, StageName.TEXT_INDEX)

ExistsPolicy = Literal["reuse_run", "new_run"]


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------


@dataclass
class StageReport:
    stage: str
    status: str
    duration_ms: int = 0
    peak_rss_mb: int = 0
    rss_delta_mb: int = 0
    error: str | None = None


@dataclass
class PageReport:
    page_number: int
    sheet_id: uuid.UUID | None = None
    rotation_deg: int = 0
    width_px: int = 0
    height_px: int = 0
    tile_count: int = 0
    tile_max_zoom: int | None = None
    stored_objects: int = 0
    bytes_written: int = 0
    text_span_count: int = 0
    path_count: int = 0
    paths_object_key: str | None = None
    has_vector_text: bool = False
    full_page_raster: bool = False
    stages: list[StageReport] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(s.status == TaskStatus.FAILED.value for s in self.stages)

    @property
    def duration_ms(self) -> int:
        return sum(s.duration_ms for s in self.stages)


@dataclass
class IngestResult:
    document_id: uuid.UUID
    run_id: uuid.UUID
    project_id: uuid.UUID
    sha256: str
    object_key: str
    byte_size: int
    page_count: int
    status: str
    reused: bool = False
    duration_ms: int = 0
    peak_rss_mb: int = 0
    bytes_written: int = 0
    tile_count: int = 0
    text_span_count: int = 0
    paths_codec: str = ""
    pages: list[PageReport] = field(default_factory=list)

    @property
    def failed_pages(self) -> list[int]:
        return [p.page_number for p in self.pages if p.failed]

    @property
    def ok_pages(self) -> list[int]:
        return [p.page_number for p in self.pages if not p.failed]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def default_backend_factory(data: bytes, *, filename_hint: str) -> PdfBackend:
    """Import the PyMuPDF backend lazily so tests can substitute another."""
    from conduit.pdf.pymupdf_backend import PyMuPdfBackend

    return PyMuPdfBackend(data, filename_hint=filename_hint)


def ingest_pdf(
    *,
    session: Session,
    store: ObjectStore,
    chunks: Iterable[bytes] | None = None,
    object_ref: ObjectRef | None = None,
    filename: str = "document.pdf",
    project_id: uuid.UUID | None = None,
    project_name: str = "Untitled project",
    document_name: str | None = None,
    cfg: StageAConfig | None = None,
    if_exists: ExistsPolicy = "reuse_run",
    triggered_by: str | None = None,
    backend_factory: Callable[..., PdfBackend] = default_backend_factory,
) -> IngestResult:
    """Run stage A over one PDF. Either ``chunks`` or ``object_ref`` is required.

    ``chunks`` is streamed into the object store with the sha256 computed in
    flight — the bytes are never buffered whole to be hashed and then written
    again.
    """
    cfg = cfg or StageAConfig()
    if (chunks is None) == (object_ref is None):
        raise ValueError("pass exactly one of chunks= or object_ref=")

    ref = object_ref or store.put_stream(chunks or (), content_type="application/pdf")
    project = _resolve_project(session, project_id=project_id, project_name=project_name)

    existing = session.execute(
        select(Document).where(
            Document.project_id == project.id, Document.sha256 == ref.sha256
        )
    ).scalar_one_or_none()

    if existing is not None and if_exists == "reuse_run":
        reusable = session.execute(
            select(PipelineRun)
            .where(
                PipelineRun.document_id == existing.id,
                PipelineRun.render_dpi == cfg.render_dpi,
                PipelineRun.status.in_(
                    [RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ERRORS]
                ),
            )
            .order_by(PipelineRun.created_at.desc())
        ).scalars().first()
        if reusable is not None:
            return IngestResult(
                document_id=existing.id,
                run_id=reusable.id,
                project_id=project.id,
                sha256=ref.sha256,
                object_key=ref.key,
                byte_size=existing.byte_size,
                page_count=existing.page_count,
                status=reusable.status.value,
                reused=True,
            )

    data = store.get_bytes(ref.key)
    with backend_factory(data, filename_hint=filename) as backend:
        del data
        info = backend.document_info()
        document = existing or _create_document(
            session,
            project=project,
            ref=ref,
            filename=filename,
            document_name=document_name or filename,
            page_count=info.page_count,
            uploaded_by=triggered_by,
        )
        run = _create_run(session, document=document, cfg=cfg, triggered_by=triggered_by)
        return _process_document(
            session=session,
            store=store,
            backend=backend,
            document=document,
            run=run,
            ref=ref,
            cfg=cfg,
        )


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def _resolve_project(
    session: Session, *, project_id: uuid.UUID | None, project_name: str
) -> Project:
    if project_id is not None:
        project = session.get(Project, project_id)
        if project is None:
            raise ConduitError(f"project {project_id} does not exist")
        return project
    project = session.execute(
        select(Project).where(Project.name == project_name)
    ).scalars().first()
    if project is None:
        project = Project(name=project_name, status=ProjectStatus.ACTIVE)
        session.add(project)
        session.flush()
    return project


def _create_document(
    session: Session,
    *,
    project: Project,
    ref: ObjectRef,
    filename: str,
    document_name: str,
    page_count: int,
    uploaded_by: str | None,
) -> Document:
    document = Document(
        project_id=project.id,
        name=document_name[:255],
        original_filename=filename[:512],
        object_key=ref.key,
        sha256=ref.sha256,
        byte_size=ref.size_bytes,
        page_count=page_count,
        uploaded_by=uploaded_by,
    )
    session.add(document)
    session.flush()
    _audit(
        session,
        event_type="document.ingested",
        entity_type="document",
        entity_id=document.id,
        document_id=document.id,
        payload={
            "object_key": ref.key,
            "sha256": ref.sha256,
            "byte_size": ref.size_bytes,
            "page_count": page_count,
        },
    )
    return document


def _create_run(
    session: Session, *, document: Document, cfg: StageAConfig, triggered_by: str | None
) -> PipelineRun:
    from conduit.ingest.paths_dump import available_codec
    from conduit.pdf import pymupdf_backend

    run = PipelineRun(
        document_id=document.id,
        status=RunStatus.QUEUED,
        stages_executed=[s.value for s in STAGE_A_STAGES],
        inherited_stages=[],
        code_version=CODE_VERSION,
        model_versions={
            "pdf_backend": f"{pymupdf_backend.BACKEND_NAME}-{pymupdf_backend.BACKEND_VERSION}",
            "text": MERGE_VERSION,
            "paths_codec": available_codec(cfg.paths_codec),
            "tiles": f"webp-q{cfg.webp_quality}-{cfg.tile_size}px",
        },
        render_dpi=cfg.render_dpi,
        page_count=document.page_count,
        triggered_by=triggered_by,
    )
    session.add(run)
    session.flush()
    _audit(
        session,
        event_type="run.created",
        entity_type="pipeline_run",
        entity_id=run.id,
        document_id=document.id,
        pipeline_run_id=run.id,
        payload={"stages": run.stages_executed, "render_dpi": cfg.render_dpi},
    )
    session.commit()
    return run


def _audit(
    session: Session,
    *,
    event_type: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    document_id: uuid.UUID | None = None,
    pipeline_run_id: uuid.UUID | None = None,
    payload: dict | None = None,
) -> None:
    session.add(
        AuditEvent(
            seq=next_audit_seq(session),
            occurred_at=utcnow(),
            document_id=document_id,
            pipeline_run_id=pipeline_run_id,
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            actor_type=ActorType.SYSTEM,
            actor_id=CODE_VERSION,
            payload=payload,
        )
    )


# ---------------------------------------------------------------------------
# The page loop
# ---------------------------------------------------------------------------


def _process_document(
    *,
    session: Session,
    store: ObjectStore,
    backend: PdfBackend,
    document: Document,
    run: PipelineRun,
    ref: ObjectRef,
    cfg: StageAConfig,
) -> IngestResult:
    run.status = RunStatus.RUNNING
    run.started_at = utcnow()
    session.commit()

    result = IngestResult(
        document_id=document.id,
        run_id=run.id,
        project_id=document.project_id,
        sha256=ref.sha256,
        object_key=ref.key,
        byte_size=ref.size_bytes,
        page_count=document.page_count,
        status=RunStatus.RUNNING.value,
    )

    try:
        for page in range(1, document.page_count + 1):
            report = _process_page(
                session=session,
                store=store,
                backend=backend,
                document=document,
                run=run,
                cfg=cfg,
                page=page,
            )
            result.pages.append(report)
            result.bytes_written += report.bytes_written
            result.tile_count += report.tile_count
            result.text_span_count += report.text_span_count
            # Commit per page: bounded memory, and a killed process leaves a
            # run whose progress is true up to the last completed page.
            session.commit()
    except BaseException as exc:  # document-level: the run is over
        session.rollback()
        run.status = RunStatus.FAILED
        run.finished_at = utcnow()
        run.error_summary = _format_error(exc)[:8000]
        session.commit()
        raise

    failed = result.failed_pages
    run.status = RunStatus.COMPLETED_WITH_ERRORS if failed else RunStatus.COMPLETED
    run.finished_at = utcnow()
    run.error_summary = (
        f"{len(failed)} of {document.page_count} pages failed: {failed}" if failed else None
    )
    run.progress = {
        "stage": "stage_a",
        "pages_total": document.page_count,
        "pages_done": len(result.ok_pages),
        "pages_failed": len(failed),
        "tiles_written": result.tile_count,
        "text_spans": result.text_span_count,
    }
    _promote(session, document=document, run=run)

    result.status = run.status.value
    result.duration_ms = _run_duration_ms(run)
    result.peak_rss_mb = process_peak_rss_mb()
    result.paths_codec = str(run.model_versions.get("paths_codec", ""))

    _audit(
        session,
        event_type="run.completed",
        entity_type="pipeline_run",
        entity_id=run.id,
        document_id=document.id,
        pipeline_run_id=run.id,
        payload={
            "status": run.status.value,
            "failed_pages": failed,
            "tiles_written": result.tile_count,
            "text_spans": result.text_span_count,
            "bytes_written": result.bytes_written,
            "peak_rss_mb": result.peak_rss_mb,
        },
    )
    session.commit()
    return result


def _run_duration_ms(run: PipelineRun) -> int:
    if run.started_at is None or run.finished_at is None:
        return 0
    start, end = run.started_at, run.finished_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return int(round((end - start).total_seconds() * 1000))


def _promote(session: Session, *, document: Document, run: PipelineRun) -> None:
    """Make this run the served one. Partial unique index: clear others first."""
    for other in session.execute(
        select(PipelineRun).where(
            PipelineRun.document_id == document.id,
            PipelineRun.is_current.is_(True),
            PipelineRun.id != run.id,
        )
    ).scalars():
        other.is_current = False
    session.flush()
    run.is_current = True
    document.current_run_id = run.id
    session.flush()


def _run_task(work: Callable[[], None]) -> tuple[StageTiming, str | None, TaskStatus]:
    """Run one page/stage task: time it, and convert a failure into a record.

    ``PixmapBudgetExceeded`` is deliberately NOT caught. A page that cannot be
    parsed is an expected fact about the world; two live pixmaps is a bug in
    this codebase, and swallowing it per page would turn the memory contract
    into a suggestion.

    The timing is read **after** the ``stage_timer`` context exits, because
    that is when ``duration_ms`` is filled in.
    """
    error: str | None = None
    status = TaskStatus.DONE
    with stage_timer() as timing:
        try:
            work()
        except PixmapBudgetExceeded:
            raise
        except PageLevelError as exc:
            status, error = TaskStatus.FAILED, _format_error(exc)
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            status, error = TaskStatus.FAILED, _format_error(exc)
    return timing, error, status


def _process_page(
    *,
    session: Session,
    store: ObjectStore,
    backend: PdfBackend,
    document: Document,
    run: PipelineRun,
    cfg: StageAConfig,
    page: int,
) -> PageReport:
    report = PageReport(page_number=page)
    state: dict[str, object] = {}

    # --- geometry, without rendering anything -----------------------------
    def read_geometry() -> None:
        geometry = backend.page_geometry(page, dpi=cfg.render_dpi)
        sheet = _upsert_sheet(session, document=document, geometry=geometry)
        state["geometry"] = geometry
        state["sheet"] = sheet
        report.sheet_id = sheet.id
        report.rotation_deg = geometry.rotation_deg
        report.width_px = geometry.width_px
        report.height_px = geometry.height_px

    timing, error, status = _run_task(read_geometry)
    if status is TaskStatus.FAILED:
        _record_task(
            session, run=run, sheet=None, page=page, stage=StageName.RASTERISE,
            status=TaskStatus.FAILED, timing=timing, error=error,
        )
        _record_task(
            session, run=run, sheet=None, page=page, stage=StageName.TEXT_INDEX,
            status=TaskStatus.SKIPPED, timing=timing,
            error="skipped: page geometry unreadable",
        )
        report.stages.append(_stage_report(StageName.RASTERISE, status, timing, error))
        _audit(
            session,
            event_type="sheet.ingest_failed",
            entity_type="sheet",
            entity_id=None,
            document_id=document.id,
            pipeline_run_id=run.id,
            payload={
                "page_number": page,
                "stage": StageName.RASTERISE.value,
                "failed_stages": _failed_stage_summaries(report),
                "traceback_location": (
                    f"page_task_state(pipeline_run_id={run.id}, page_number={page})"
                ),
            },
        )
        return report

    sheet = state["sheet"]  # type: ignore[assignment]
    geometry = state["geometry"]  # type: ignore[assignment]
    artifacts: dict[str, object] = {}

    # --- rasterise: tiles, thumbnail, whole-page PNG, vector paths --------
    def rasterise() -> None:
        if cfg.write_tiles:
            pyramid = build_pyramid(backend, geometry, store, cfg)
            sheet.tile_base_key = pyramid.manifest_key
            sheet.tile_max_zoom = pyramid.max_zoom
            sheet.thumbnail_object_key = pyramid.thumbnail_key
            sheet.raster_run_id = run.id
            report.tile_count = pyramid.tile_count
            report.tile_max_zoom = pyramid.max_zoom
            report.stored_objects += pyramid.stored_object_count
            report.bytes_written += pyramid.bytes_written
            artifacts["tile_manifest_key"] = pyramid.manifest_key
            artifacts["thumbnail_key"] = pyramid.thumbnail_key
            artifacts["tile_count"] = pyramid.tile_count

        full = render_full_page(backend, geometry, store, cfg)
        if full is not None:
            sheet.raster_object_key, raster_bytes = full
            report.bytes_written += raster_bytes
            report.full_page_raster = True
            artifacts["raster_object_key"] = sheet.raster_object_key
        else:
            artifacts["raster_object_key"] = None
            artifacts["raster_skipped_reason"] = (
                f"{geometry.width_px}x{geometry.height_px}px exceeds "
                f"full_page_raster_max_pixels={cfg.full_page_raster_max_pixels}"
            )

        artifact = dump_paths(
            backend.drawings(page),
            page_number=page,
            extractor_version=str(run.model_versions.get("pdf_backend", "unknown")),
            store=store,
            cfg=cfg,
        )
        report.path_count = artifact.path_count
        report.bytes_written += artifact.ref.size_bytes
        # Queryable provenance, not just an audit payload key (AGENTS.md §5).
        sheet.paths_object_key = artifact.ref.key
        report.paths_object_key = artifact.ref.key
        artifacts["paths_object_key"] = artifact.ref.key
        artifacts["paths_codec"] = artifact.codec
        artifacts["path_count"] = artifact.path_count

    timing, error, status = _run_task(rasterise)
    _record_task(
        session, run=run, sheet=sheet, page=page, stage=StageName.RASTERISE,
        status=status, timing=timing, error=error,
    )
    report.stages.append(_stage_report(StageName.RASTERISE, status, timing, error))

    # --- text index: merged TextSpan rows ---------------------------------
    def text_index() -> None:
        count = _write_text_spans(
            session, run=run, sheet=sheet, page=page, spans=backend.text_spans(page)
        )
        sheet.text_span_count = count
        sheet.has_vector_text = count > 0
        report.text_span_count = count
        report.has_vector_text = count > 0

    timing, error, status = _run_task(text_index)
    _record_task(
        session, run=run, sheet=sheet, page=page, stage=StageName.TEXT_INDEX,
        status=status, timing=timing, error=error,
    )
    report.stages.append(_stage_report(StageName.TEXT_INDEX, status, timing, error))

    # A page that failed any stage does NOT get a success event. An audit trail
    # that reads `sheet.ingested` for a page whose raster and text never
    # materialised is worse than silence: it tells a reviewer the page is fine.
    # Exactly one of these two events is emitted per page, always.
    payload = {
        "page_number": page,
        "rotation_deg": report.rotation_deg,
        "width_px": report.width_px,
        "height_px": report.height_px,
        "render_dpi": cfg.render_dpi,
        "text_span_count": report.text_span_count,
        **artifacts,
    }
    if report.failed:
        payload["failed_stages"] = _failed_stage_summaries(report)
        payload["traceback_location"] = (
            f"page_task_state(pipeline_run_id={run.id}, page_number={page})"
        )
    _audit(
        session,
        event_type="sheet.ingest_failed" if report.failed else "sheet.ingested",
        entity_type="sheet",
        entity_id=report.sheet_id,
        document_id=document.id,
        pipeline_run_id=run.id,
        payload=payload,
    )
    return report


def _failed_stage_summaries(report: PageReport) -> list[dict[str, str]]:
    """One line per failed stage: which stage, and the exception line.

    The full traceback stays on ``PageTaskState.error`` — the audit payload
    carries enough to know *what* broke plus where to read the rest, so the
    audit table does not become a log store.
    """
    out: list[dict[str, str]] = []
    for stage in report.stages:
        if stage.status != TaskStatus.FAILED.value:
            continue
        last = (stage.error or "").strip().splitlines()
        out.append({"stage": stage.stage, "error": last[-1] if last else "unknown error"})
    return out


def _stage_report(
    stage: StageName, status: TaskStatus, timing: StageTiming, error: str | None
) -> StageReport:
    return StageReport(
        stage=stage.value,
        status=status.value,
        duration_ms=timing.duration_ms,
        peak_rss_mb=timing.peak_rss_mb,
        rss_delta_mb=timing.rss_delta_mb,
        error=error,
    )


def _format_error(exc: BaseException) -> str:
    """Full traceback, stored verbatim on ``PageTaskState.error``.

    A one-line message is not enough to debug a page failure six weeks later,
    and the roadmap's DoD says each failed page carries *its traceback*.
    """
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _upsert_sheet(session: Session, *, document: Document, geometry: PageGeometry) -> Sheet:
    """Create or refresh the ``Sheet`` row for one page.

    Geometry comes from the page dictionary, never from a render — which is why
    a page whose *content* is corrupt still gets a real ``Sheet`` row, and its
    failure is attributable to a page rather than showing up as a hole in the
    page sequence.
    """
    sheet = session.execute(
        select(Sheet).where(
            Sheet.document_id == document.id, Sheet.page_number == geometry.page_number
        )
    ).scalar_one_or_none()
    if sheet is None:
        sheet = Sheet(document_id=document.id, page_number=geometry.page_number)
        session.add(sheet)
    sheet.media_box_x0 = geometry.media_box_x0
    sheet.media_box_y0 = geometry.media_box_y0
    sheet.media_box_x1 = geometry.media_box_x1
    sheet.media_box_y1 = geometry.media_box_y1
    sheet.rotation_deg = geometry.rotation_deg
    sheet.render_dpi = geometry.render_dpi
    sheet.width_px = geometry.width_px
    sheet.height_px = geometry.height_px
    session.flush()
    return sheet


def _write_text_spans(
    session: Session, *, run: PipelineRun, sheet: Sheet, page: int, spans: Sequence
) -> int:
    """Merge glyph runs into lines and persist one row per merged line."""
    existing = session.execute(
        select(func.count())
        .select_from(TextSpan)
        .where(TextSpan.pipeline_run_id == run.id, TextSpan.sheet_id == sheet.id)
    ).scalar_one()
    if existing:
        return int(existing)

    rows = []
    for line in merge_page_spans(list(spans)):
        rows.append(
            TextSpan(
                pipeline_run_id=run.id,
                sheet_id=sheet.id,
                page_number=page,
                text=line.text,
                normalized_text=line.normalized_text,
                coordinate_space=CoordinateSpace.PDF_POINTS,
                bbox_x0=line.bbox.x0,
                bbox_y0=line.bbox.y0,
                bbox_x1=line.bbox.x1,
                bbox_y1=line.bbox.y1,
                rotation_deg=line.rotation_deg,
                font_name=(line.font_name or None) and line.font_name[:128],
                font_size_pt=line.font_size_pt,
                source=TextSource.VECTOR,
                confidence=1.0,
                role=SpanRole.UNKNOWN,
            )
        )
    session.add_all(rows)
    session.flush()
    return len(rows)


def _record_task(
    session: Session,
    *,
    run: PipelineRun,
    sheet: Sheet | None,
    page: int,
    stage: StageName,
    status: TaskStatus,
    timing: StageTiming,
    error: str | None,
) -> PageTaskState:
    task = session.execute(
        select(PageTaskState).where(
            PageTaskState.pipeline_run_id == run.id,
            PageTaskState.page_number == page,
            PageTaskState.stage == stage,
        )
    ).scalar_one_or_none()
    if task is None:
        task = PageTaskState(pipeline_run_id=run.id, page_number=page, stage=stage)
        session.add(task)
    task.sheet_id = sheet.id if sheet is not None else None
    task.status = status
    task.attempts = (task.attempts or 0) + 1
    finished = utcnow()
    task.started_at = _minus_ms(finished, timing.duration_ms)
    task.finished_at = finished
    task.duration_ms = timing.duration_ms
    task.peak_rss_mb = timing.peak_rss_mb
    task.error = error
    session.flush()
    return task


def _minus_ms(when: datetime, ms: int) -> datetime:
    from datetime import timedelta

    return when - timedelta(milliseconds=ms)
