"""Stage A end to end, checked against the corpus manifest's ground truth.

The manifest (``bench/out/synthetic_corpus.manifest.json``, and the object the
generator returns) is *structural* ground truth: page count, page size,
rotation, which pages are flattened rasters, which pages are deliberately
corrupt. Those are the only facts a synthetic corpus can honestly supply, and
they are exactly what stage A must get right. No accuracy claim is made or
possible here — see ``bench/CORPUS.md``.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from conduit.db.models import (
    AuditEvent,
    Document,
    PageTaskState,
    PipelineRun,
    RunStatus,
    Sheet,
    StageName,
    TaskStatus,
    TextSpan,
    TextSource,
)


# --- the run itself -------------------------------------------------------


def test_run_completes_with_errors_because_corrupt_pages_are_expected(ingested) -> None:
    result = ingested.result
    assert result.status == RunStatus.COMPLETED_WITH_ERRORS.value
    assert result.page_count == ingested.manifest.page_count
    assert sorted(result.failed_pages) == sorted(ingested.manifest.corrupt_pages)
    assert len(result.ok_pages) == result.page_count - len(ingested.manifest.corrupt_pages)


def test_run_row_records_versions_and_progress(ingested) -> None:
    with ingested.Session() as session:
        run = session.get(PipelineRun, ingested.result.run_id)
        assert run.status is RunStatus.COMPLETED_WITH_ERRORS
        assert run.stages_executed == ["rasterise", "text_index"]
        assert run.render_dpi == ingested.cfg.render_dpi
        assert run.started_at is not None and run.finished_at is not None
        # Provenance: which code produced these rows.
        assert run.model_versions["pdf_backend"].startswith("pymupdf-")
        assert run.model_versions["text"] == "linemerge-2"
        assert run.model_versions["paths_codec"] in {"zstd", "gzip"}
        assert run.progress["pages_total"] == ingested.manifest.page_count
        assert run.is_current is True
        assert run.error_summary and "pages failed" in run.error_summary


def test_document_row_matches_the_bytes_that_were_ingested(ingested) -> None:
    import hashlib

    with ingested.Session() as session:
        doc = session.get(Document, ingested.result.document_id)
        assert doc.sha256 == hashlib.sha256(ingested.pdf_bytes).hexdigest()
        assert doc.byte_size == len(ingested.pdf_bytes)
        assert doc.page_count == ingested.manifest.page_count
        assert doc.current_run_id == ingested.result.run_id
        # The upload is stored under its own digest and readable back byte for byte.
        assert ingested.store.get_bytes(doc.object_key) == ingested.pdf_bytes


# --- sheets: geometry read without rendering ------------------------------


def test_every_page_gets_a_sheet_row_with_manifest_geometry(ingested) -> None:
    with ingested.Session() as session:
        sheets = session.execute(
            select(Sheet)
            .where(Sheet.document_id == ingested.result.document_id)
            .order_by(Sheet.page_number)
        ).scalars().all()

    assert [s.page_number for s in sheets] == list(range(1, ingested.manifest.page_count + 1))
    for sheet, spec in zip(sheets, ingested.manifest.pages, strict=True):
        assert sheet.rotation_deg == spec["rotation"]
        assert sheet.media_box_x1 - sheet.media_box_x0 == pytest.approx(spec["width_pt"])
        assert sheet.media_box_y1 - sheet.media_box_y0 == pytest.approx(spec["height_pt"])
        assert sheet.render_dpi == ingested.cfg.render_dpi
        # /Rotate swaps the raster's axes; the MediaBox is unrotated.
        scale = ingested.cfg.render_dpi / 72.0
        long_edge = max(spec["width_pt"], spec["height_pt"]) * scale
        assert max(sheet.width_px, sheet.height_px) == pytest.approx(long_edge, abs=1)
        if spec["rotation"] in (90, 270):
            assert sheet.width_px < sheet.height_px or spec["width_pt"] < spec["height_pt"]


def test_corrupt_pages_still_get_a_sheet_row(ingested) -> None:
    """Geometry comes from the page dictionary, so a broken page is still a sheet.

    Without this, a corrupt page would be a *hole* in the page sequence and the
    failure would be unattributable — which is the opposite of auditable.
    """
    with ingested.Session() as session:
        for page in ingested.manifest.corrupt_pages:
            sheet = session.execute(
                select(Sheet).where(
                    Sheet.document_id == ingested.result.document_id,
                    Sheet.page_number == page,
                )
            ).scalar_one()
            assert sheet.media_box_x1 > sheet.media_box_x0
            assert sheet.text_span_count == 0


# --- page task state ------------------------------------------------------


def test_page_task_state_covers_every_page_and_stage(ingested) -> None:
    with ingested.Session() as session:
        tasks = session.execute(
            select(PageTaskState).where(
                PageTaskState.pipeline_run_id == ingested.result.run_id
            )
        ).scalars().all()

    pages = ingested.manifest.page_count
    assert len(tasks) == pages * 2, "one row per page per stage A stage"
    assert {t.stage for t in tasks} == {StageName.RASTERISE, StageName.TEXT_INDEX}

    failed = [t for t in tasks if t.status is TaskStatus.FAILED]
    assert sorted({t.page_number for t in failed}) == sorted(ingested.manifest.corrupt_pages)
    for task in failed:
        assert task.error and "Traceback" in task.error, "a failed page stores its traceback"
        assert "CorruptPageError" in task.error


def test_duration_and_peak_rss_are_populated_for_every_task(ingested) -> None:
    """Both columns already existed; the point of this test is that they are real."""
    with ingested.Session() as session:
        tasks = session.execute(
            select(PageTaskState).where(
                PageTaskState.pipeline_run_id == ingested.result.run_id
            )
        ).scalars().all()

    assert all(t.duration_ms is not None and t.duration_ms >= 0 for t in tasks)
    assert all(t.peak_rss_mb is not None and t.peak_rss_mb > 0 for t in tasks)
    assert all(t.started_at is not None and t.finished_at is not None for t in tasks)
    assert all(t.attempts == 1 for t in tasks)
    # Non-trivial work was actually timed on the successful rasterise tasks.
    raster_ok = [
        t
        for t in tasks
        if t.stage is StageName.RASTERISE and t.status is TaskStatus.DONE
    ]
    assert max(t.duration_ms for t in raster_ok) > 0


# --- text spans, against the manifest -------------------------------------


def test_text_span_counts_follow_the_manifest(ingested) -> None:
    """Ground truth available from a synthetic corpus:

    * flattened-raster pages have **no** vector text layer -> zero spans;
    * corrupt pages produce no spans at all;
    * every other page has spans, and its sheet number appears among them
      (the generator wrote it into the title block).
    """
    manifest = ingested.manifest
    with ingested.Session() as session:
        counts = dict(
            session.execute(
                select(TextSpan.page_number, func.count())
                .where(TextSpan.pipeline_run_id == ingested.result.run_id)
                .group_by(TextSpan.page_number)
            ).all()
        )
        sheets = {
            s.page_number: s
            for s in session.execute(
                select(Sheet).where(Sheet.document_id == ingested.result.document_id)
            ).scalars()
        }
        texts = {}
        for page in range(1, manifest.page_count + 1):
            texts[page] = [
                t
                for t in session.execute(
                    select(TextSpan.normalized_text).where(
                        TextSpan.pipeline_run_id == ingested.result.run_id,
                        TextSpan.page_number == page,
                    )
                ).scalars()
            ]

    for spec in manifest.pages:
        page = spec["page_number"]
        got = counts.get(page, 0)
        if page in manifest.corrupt_pages:
            assert got == 0, f"page {page} is corrupt; it must yield no spans"
        elif page in manifest.raster_pages:
            assert got == 0, f"page {page} is a flattened raster; it has no text layer"
            assert sheets[page].has_vector_text is False
        else:
            assert got > 0, f"page {page} is a vector page and must yield spans"
            assert sheets[page].has_vector_text is True
            if True:  # every page carries its own sheet number since slice C
                assert any(spec["sheet_number"] in t for t in texts[page]), (
                    f"page {page} should carry its own sheet number {spec['sheet_number']}"
                )
        assert sheets[page].text_span_count == got


def test_rotated_title_block_is_indexed_at_its_true_angle(ingested) -> None:
    """The sideways title block must survive ingest whole.

    It used to not: ``make_corpus`` drew the 90-degree block with
    ``insert_textbox(rotate=90)`` without rotating the rect, so the writer
    wrapped mid-token and page 7 genuinely contained ``M-1`` and ``02``. Slice
    C fixed the generator (a rotated block is now a rotated *rect*); this test
    keeps stage A honest about the angle, and
    ``tests/test_corpus_titleblock.py`` pins the generator itself.
    """
    with ingested.Session() as session:
        rotated = session.execute(
            select(TextSpan.text, TextSpan.rotation_deg).where(
                TextSpan.pipeline_run_id == ingested.result.run_id,
                TextSpan.page_number == 7,
                TextSpan.rotation_deg != 0.0,
            )
        ).all()

    assert rotated, "the rotated title block must still be indexed"
    assert all(abs(rot - 90.0) < 0.01 for _, rot in rotated)
    fragments = {text for text, _ in rotated}
    assert any("M-102" in text for text in fragments), (
        "the rotated title block must survive as unbroken tokens (slice C fixed the "
        "generator; tests/test_corpus_titleblock.py pins the corrected behaviour)"
    )
    # The font-size guard added to the merge (see textlines.py) must keep
    # different title-block fields apart.
    assert not any(text.startswith("MECHASH") for text in fragments)


def test_spans_are_merged_lines_not_glyph_runs(ingested) -> None:
    """§2.2 persists one row per merged line. The legend proves the merge ran.

    The generator writes ``"{tag}  {desc}"`` as a single ``insert_text`` call,
    but MuPDF splits runs on font metrics; what must come back out is one row
    per legend line, not one per fragment.
    """
    with ingested.Session() as session:
        rows = session.execute(
            select(TextSpan.text).where(
                TextSpan.pipeline_run_id == ingested.result.run_id,
                TextSpan.normalized_text.like("%RECESSED LUMINAIRE%"),
            )
        ).scalars().all()
    assert rows, "the legend line should be indexed"
    assert any(r.startswith("TYPE A") and "LUMINAIRE" in r for r in rows)


def test_every_span_carries_full_provenance(ingested) -> None:
    """No evidence row without sheet, page, bbox and coordinate space."""
    with ingested.Session() as session:
        spans = session.execute(
            select(TextSpan).where(TextSpan.pipeline_run_id == ingested.result.run_id)
        ).scalars().all()
        sheets = {
            s.id: s
            for s in session.execute(
                select(Sheet).where(Sheet.document_id == ingested.result.document_id)
            ).scalars()
        }

    assert spans
    for span in spans:
        assert span.sheet_id in sheets
        assert span.page_number == sheets[span.sheet_id].page_number
        assert span.coordinate_space.value == "pdf_points"
        assert span.bbox_x1 >= span.bbox_x0 and span.bbox_y1 >= span.bbox_y0
        assert span.source is TextSource.VECTOR
        assert span.confidence == 1.0
        # pdf_points are MediaBox-relative: a span must lie on its own page.
        sheet = sheets[span.sheet_id]
        assert sheet.media_box_x0 - 1 <= span.bbox_x0 <= sheet.media_box_x1 + 1
        assert sheet.media_box_y0 - 1 <= span.bbox_y0 <= sheet.media_box_y1 + 1


# --- artifacts ------------------------------------------------------------


def test_artifacts_exist_in_the_store_for_every_good_page(ingested) -> None:
    with ingested.Session() as session:
        sheets = session.execute(
            select(Sheet)
            .where(Sheet.document_id == ingested.result.document_id)
            .order_by(Sheet.page_number)
        ).scalars().all()

    for sheet in sheets:
        if sheet.page_number in ingested.manifest.corrupt_pages:
            assert sheet.tile_base_key is None
            continue
        assert sheet.tile_base_key and ingested.store.exists(sheet.tile_base_key)
        assert sheet.thumbnail_object_key and ingested.store.exists(
            sheet.thumbnail_object_key
        )
        assert sheet.raster_run_id == ingested.result.run_id
        assert sheet.tile_max_zoom is not None and sheet.tile_max_zoom >= 1


def test_paths_artifact_is_readable_and_in_pdf_points(ingested) -> None:
    from conduit.ingest.paths_dump import load_paths

    with ingested.Session() as session:
        events = session.execute(
            select(AuditEvent).where(
                AuditEvent.pipeline_run_id == ingested.result.run_id,
                AuditEvent.event_type == "sheet.ingested",
            )
        ).scalars().all()

    # sheet.ingested now fires only for pages that actually ingested; a failed
    # page emits sheet.ingest_failed instead (tests/test_stage_a_provenance.py).
    ok = list(events)
    assert len(ok) == ingested.manifest.page_count - len(ingested.manifest.corrupt_pages)
    sample = ok[0]
    blob = ingested.store.get_bytes(sample.payload["paths_object_key"])
    doc = load_paths(blob, sample.payload["paths_codec"])
    assert doc["coordinate_space"] == "pdf_points"
    assert doc["page_number"] == sample.payload["page_number"]
    assert doc["path_count"] == len(doc["paths"]) > 0
    assert doc["extractor_version"].startswith("pymupdf-")


def test_audit_trail_names_every_artifact(ingested) -> None:
    with ingested.Session() as session:
        events = session.execute(
            select(AuditEvent).order_by(AuditEvent.seq)
        ).scalars().all()

    types = [e.event_type for e in events]
    assert types[0] == "document.ingested"
    assert "run.created" in types and types[-1] == "run.completed"
    assert types.count("sheet.ingested") + types.count("sheet.ingest_failed") == (
        ingested.manifest.page_count
    )
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    completed = events[-1]
    assert completed.payload["status"] == RunStatus.COMPLETED_WITH_ERRORS.value
    assert completed.payload["failed_pages"] == ingested.manifest.corrupt_pages


# --- idempotence ----------------------------------------------------------


def _object_count(env) -> int:
    return sum(1 for p in (env.store.root).rglob("*") if p.is_file() and p.parent.name != "tmp")


def test_reingesting_the_same_bytes_creates_nothing_new(ingested) -> None:
    """Same bytes -> same sha256 -> same key -> no second Document, no new blobs."""
    before = _object_count(ingested)
    with ingested.Session() as session:
        docs_before = session.execute(select(func.count()).select_from(Document)).scalar_one()
        runs_before = session.execute(select(func.count()).select_from(PipelineRun)).scalar_one()
        spans_before = session.execute(select(func.count()).select_from(TextSpan)).scalar_one()

    from conduit.ingest import ingest_pdf

    with ingested.Session() as session:
        again = ingest_pdf(
            session=session,
            store=ingested.store,
            chunks=[ingested.pdf_bytes],
            filename="synthetic_corpus.pdf",
            project_name="stage-a-tests",
            cfg=ingested.cfg,
        )

    assert again.reused is True
    assert again.document_id == ingested.result.document_id
    assert again.run_id == ingested.result.run_id
    assert again.sha256 == ingested.result.sha256

    with ingested.Session() as session:
        assert (
            session.execute(select(func.count()).select_from(Document)).scalar_one()
            == docs_before
        )
        assert (
            session.execute(select(func.count()).select_from(PipelineRun)).scalar_one()
            == runs_before
        )
        assert (
            session.execute(select(func.count()).select_from(TextSpan)).scalar_one()
            == spans_before
        )
    assert _object_count(ingested) == before


def test_a_forced_new_run_reuses_every_blob(ingested) -> None:
    """Interpretation is versioned; evidence bytes are not re-stored.

    A second run over identical bytes must write new rows (a new run's
    evidence) and **zero** new objects: every raster, tile and paths blob
    hashes to a key that already exists.
    """
    from conduit.ingest import ingest_pdf

    before = _object_count(ingested)
    with ingested.Session() as session:
        second = ingest_pdf(
            session=session,
            store=ingested.store,
            chunks=[ingested.pdf_bytes],
            filename="synthetic_corpus.pdf",
            project_name="stage-a-tests",
            cfg=ingested.cfg,
            if_exists="new_run",
        )

    assert second.reused is False
    assert second.document_id == ingested.result.document_id
    assert second.run_id != ingested.result.run_id
    assert _object_count(ingested) == before, "content addressing must dedupe every artifact"

    with ingested.Session() as session:
        doc = session.get(Document, second.document_id)
        assert doc.current_run_id == second.run_id
        currents = session.execute(
            select(PipelineRun).where(
                PipelineRun.document_id == doc.id, PipelineRun.is_current.is_(True)
            )
        ).scalars().all()
        assert [r.id for r in currents] == [second.run_id], "exactly one current run"
        # The first run's evidence is untouched — it is still fully queryable.
        old_spans = session.execute(
            select(func.count())
            .select_from(TextSpan)
            .where(TextSpan.pipeline_run_id == ingested.result.run_id)
        ).scalar_one()
        assert old_spans == ingested.result.text_span_count


def test_tile_manifest_shape(ingested) -> None:
    with ingested.Session() as session:
        sheet = session.execute(
            select(Sheet).where(
                Sheet.document_id == ingested.result.document_id, Sheet.page_number == 1
            )
        ).scalar_one()
    manifest = json.loads(ingested.store.get_bytes(sheet.tile_base_key))
    assert manifest["schema"] == "conduit.tile_manifest/1"
    assert manifest["render_dpi"] == ingested.cfg.render_dpi
    assert manifest["base_width_px"] == sheet.width_px
    assert manifest["base_height_px"] == sheet.height_px
    assert manifest["max_zoom"] == sheet.tile_max_zoom
    assert [lvl["z"] for lvl in manifest["levels"]] == list(range(sheet.tile_max_zoom + 1))
    # Every tile named in the manifest is really in the store.
    top = manifest["levels"][0]
    assert top["cols"] == top["rows"] == 1
    for key in top["tiles"].values():
        assert ingested.store.exists(key)
