"""Two things a reviewer must be able to do from the database alone.

1. **See that a page failed.** Before this file existed, a run over the corpus
   wrote ``sheet.ingested`` for all 24 pages — including the two whose raster
   and text never materialised. The tracebacks were on ``page_task_state``, but
   an auditor reading ``audit_event`` would have concluded every page ingested
   cleanly. An audit trail that omits a failure is worse than one that says
   nothing: it actively asserts something false.
2. **Query where a page's vector paths went.** The ``paths.json.zst`` key used
   to live only inside an audit payload. Traceable, but not queryable —
   "which sheets have no path dump" was a JSON scan. ``AGENTS.md`` §5: if a
   reviewer needs it, give it a column.
"""

from __future__ import annotations

from sqlalchemy import func, select

from conduit.db.models import AuditEvent, PageTaskState, Sheet, TaskStatus


def _payload_pages(session, run_id, event_type: str) -> list[int]:
    rows = session.execute(
        select(AuditEvent.payload).where(
            AuditEvent.pipeline_run_id == run_id, AuditEvent.event_type == event_type
        )
    ).scalars()
    return sorted(int(p["page_number"]) for p in rows)


def test_failed_pages_emit_a_failure_event(ingested) -> None:
    corrupt = sorted(ingested.manifest.corrupt_pages)
    assert corrupt, "the corpus must contain deliberately corrupt pages"
    session = ingested.Session()
    try:
        failed = _payload_pages(session, ingested.result.run_id, "sheet.ingest_failed")
    finally:
        session.close()
    assert failed == corrupt, (
        "every page that failed must emit sheet.ingest_failed; "
        f"expected {corrupt}, audit trail says {failed}"
    )


def test_failed_pages_emit_no_success_event(ingested) -> None:
    """The other half: a failed page must NOT also look ingested."""
    corrupt = set(ingested.manifest.corrupt_pages)
    session = ingested.Session()
    try:
        ingested_pages = set(_payload_pages(session, ingested.result.run_id, "sheet.ingested"))
    finally:
        session.close()
    assert not (ingested_pages & corrupt), (
        f"pages {sorted(ingested_pages & corrupt)} failed but were reported as ingested"
    )
    assert len(ingested_pages) == ingested.result.page_count - len(corrupt)


def test_every_page_emits_exactly_one_terminal_event(ingested) -> None:
    session = ingested.Session()
    try:
        ok = _payload_pages(session, ingested.result.run_id, "sheet.ingested")
        bad = _payload_pages(session, ingested.result.run_id, "sheet.ingest_failed")
    finally:
        session.close()
    assert sorted(ok + bad) == list(range(1, ingested.result.page_count + 1))
    assert not set(ok) & set(bad)


def test_failure_event_names_the_stage_and_where_the_traceback_is(ingested) -> None:
    session = ingested.Session()
    try:
        payloads = session.execute(
            select(AuditEvent.payload).where(
                AuditEvent.pipeline_run_id == ingested.result.run_id,
                AuditEvent.event_type == "sheet.ingest_failed",
            )
        ).scalars().all()
        for payload in payloads:
            stages = payload["failed_stages"]
            assert stages, "a failure event must name at least one failed stage"
            assert all(s["error"] for s in stages), "each failed stage carries its error line"
            assert "page_task_state" in payload["traceback_location"]
            stored = session.execute(
                select(PageTaskState.error).where(
                    PageTaskState.pipeline_run_id == ingested.result.run_id,
                    PageTaskState.page_number == payload["page_number"],
                    PageTaskState.status == TaskStatus.FAILED,
                )
            ).scalars().all()
            assert stored and all(e and "Traceback" in e for e in stored), (
                "the audit event points at the traceback; the traceback must be there"
            )
    finally:
        session.close()


def test_paths_object_key_is_a_queryable_column(ingested) -> None:
    corrupt = set(ingested.manifest.corrupt_pages)
    session = ingested.Session()
    try:
        rows = session.execute(
            select(Sheet.page_number, Sheet.paths_object_key)
            .where(Sheet.document_id == ingested.result.document_id)
            .order_by(Sheet.page_number)
        ).all()
        # The query a reviewer would actually run.
        without = session.execute(
            select(func.count())
            .select_from(Sheet)
            .where(
                Sheet.document_id == ingested.result.document_id,
                Sheet.paths_object_key.is_(None),
            )
        ).scalar_one()
    finally:
        session.close()

    keyed = {page for page, key in rows if key}
    assert keyed == {p for p, _ in rows} - corrupt, (
        "every page whose rasterise stage succeeded must carry its paths key"
    )
    assert int(without) == len(corrupt)


def test_paths_object_key_matches_the_audit_payload(ingested) -> None:
    """The column and the payload must agree — one is not allowed to drift."""
    session = ingested.Session()
    try:
        payloads = session.execute(
            select(AuditEvent.payload).where(
                AuditEvent.pipeline_run_id == ingested.result.run_id,
                AuditEvent.event_type == "sheet.ingested",
            )
        ).scalars().all()
        by_page = {int(p["page_number"]): p.get("paths_object_key") for p in payloads}
        rows = session.execute(
            select(Sheet.page_number, Sheet.paths_object_key).where(
                Sheet.document_id == ingested.result.document_id
            )
        ).all()
    finally:
        session.close()
    for page, key in rows:
        if page in by_page:
            assert key == by_page[page], f"page {page}: column {key!r} != payload {by_page[page]!r}"


def test_stored_paths_object_is_readable_through_the_key(ingested) -> None:
    from conduit.ingest.paths_dump import PATHS_SCHEMA, decode_paths_blob

    session = ingested.Session()
    try:
        key, page = session.execute(
            select(Sheet.paths_object_key, Sheet.page_number)
            .where(
                Sheet.document_id == ingested.result.document_id,
                Sheet.paths_object_key.is_not(None),
            )
            .order_by(Sheet.page_number)
            .limit(1)
        ).one()
    finally:
        session.close()
    payload = decode_paths_blob(ingested.store.get_bytes(key))
    assert payload["schema"] == PATHS_SCHEMA
    assert payload["page_number"] == page
    assert payload["coordinate_space"] == "pdf_points"
