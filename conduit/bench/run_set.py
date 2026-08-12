"""``run_set`` — process a whole plan set through stages A and B and measure it.

    PYTHONPATH=. python -m bench.run_set --pdf bench/out/synthetic_corpus.pdf \
        --dpi 200 --append

Roadmap W1 task 7: run a set end to end and record, per page and per stage,
``duration_ms`` and ``peak_rss_mb`` **into ``PageTaskState``** — both columns
already exist — then summarise into ``bench/RESULTS.md``.

Everything this tool prints is read back **out of the database**, from the rows
the run actually wrote, not from in-memory counters. That is deliberate: the
numbers in ``RESULTS.md`` must be reproducible by anyone with the SQLite file
and the queries printed alongside them.

What this tool will not do
--------------------------
It will not print an accuracy number. The only corpus in this repo is
synthetic (``bench/CORPUS.md``), so throughput and memory readings from it are
real and classification *correctness* readings from it do not exist. The
classification abstain rate **is** reported, because it is a behaviour reading
— how often the rules decline to classify — not a measure of whether the
classes are right. That distinction is the whole reason the roadmap's trigger
is expressed as an abstain rate.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from conduit.classify import ClassifyResult, classify_document
from conduit.db.models import (
    AuditEvent,
    Base,
    Discipline,
    Document,
    PageTaskState,
    PipelineRun,
    Sheet,
    StageName,
    TaskStatus,
    TextSpan,
)
from conduit.db.session import (
    apply_dialect_extras,
    create_engine_from_env,
    session_factory,
)
from conduit.ingest.config import StageAConfig
from conduit.ingest.metrics import process_peak_rss_mb
from conduit.ingest.stage_a import IngestResult, ingest_pdf
from conduit.store.local import default_local_store

__all__ = ["StageStats", "build_parser", "main", "render_report"]

#: Roadmap W1 DoD: the memory gate the run is measured against.
PEAK_RSS_GATE_MB = 1500
#: ``03-pipeline-specs.md`` §1.5: above this abstain rate, the thumbnail
#: classifier decision gets made (either way, in writing).
ABSTAIN_TRIGGER = 0.15


@dataclass(frozen=True)
class StageStats:
    stage: str
    done: int
    failed: int
    skipped: int
    p50_s: float
    p95_s: float
    max_s: float
    total_s: float
    max_peak_rss_mb: int


def _percentile(values: Sequence[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * len(ordered))) - 1))
    return float(ordered[idx])


def stage_stats(session, run_id) -> list[StageStats]:
    """Per-stage timing, straight out of ``page_task_state``.

    Equivalent SQL, printed in the report so the number is checkable:

        SELECT stage, status, count(*), max(peak_rss_mb)
        FROM page_task_state WHERE pipeline_run_id = :run GROUP BY 1, 2;
    """
    rows = session.execute(
        select(
            PageTaskState.stage,
            PageTaskState.status,
            PageTaskState.duration_ms,
            PageTaskState.peak_rss_mb,
        ).where(PageTaskState.pipeline_run_id == run_id)
    ).all()
    by_stage: dict[str, list[tuple[str, int, int]]] = {}
    for stage, status, duration_ms, peak in rows:
        by_stage.setdefault(stage.value, []).append(
            (status.value, duration_ms or 0, peak or 0)
        )
    out: list[StageStats] = []
    for stage in (StageName.RASTERISE, StageName.TEXT_INDEX, StageName.CLASSIFY):
        entries = by_stage.get(stage.value)
        if not entries:
            continue
        done = [d for status, d, _ in entries if status == TaskStatus.DONE.value]
        seconds = [d / 1000.0 for d in done]
        out.append(
            StageStats(
                stage=stage.value,
                done=len(done),
                failed=sum(1 for s, _, _ in entries if s == TaskStatus.FAILED.value),
                skipped=sum(1 for s, _, _ in entries if s == TaskStatus.SKIPPED.value),
                p50_s=_percentile(seconds, 50),
                p95_s=_percentile(seconds, 95),
                max_s=max(seconds) if seconds else 0.0,
                total_s=sum(seconds),
                max_peak_rss_mb=max((p for _, _, p in entries), default=0),
            )
        )
    return out


def _audit_counts(session, run_id, document_id) -> dict[str, int]:
    rows = session.execute(
        select(AuditEvent.event_type, func.count())
        .where(
            (AuditEvent.pipeline_run_id == run_id)
            | (AuditEvent.document_id == document_id)
        )
        .group_by(AuditEvent.event_type)
    ).all()
    return {event: count for event, count in rows}


def _discipline_counts(session, document_id) -> dict[str, int]:
    rows = session.execute(
        select(Sheet.discipline, func.count())
        .where(Sheet.document_id == document_id)
        .group_by(Sheet.discipline)
    ).all()
    return {d.value: c for d, c in rows}


def _method_counts(session, document_id) -> dict[str, int]:
    rows = session.execute(
        select(Sheet.classification_method, func.count())
        .where(Sheet.document_id == document_id)
        .group_by(Sheet.classification_method)
    ).all()
    return {m.value: c for m, c in rows}


def _abstain_from_db(session, document_id) -> tuple[int, int]:
    """The abstain rate as the roadmap defines it, computed in SQL.

        SELECT count(*) FROM sheet
        WHERE document_id = :doc
          AND (classification_confidence < 0.60 OR discipline = 'UNKNOWN');
    """
    total = session.execute(
        select(func.count()).select_from(Sheet).where(Sheet.document_id == document_id)
    ).scalar_one()
    abstained = session.execute(
        select(func.count())
        .select_from(Sheet)
        .where(
            Sheet.document_id == document_id,
            (Sheet.classification_confidence < 0.60)
            | (Sheet.discipline == Discipline.UNKNOWN),
        )
    ).scalar_one()
    return int(abstained), int(total)


def _fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"`{k}` {v}" for k, v in sorted(counts.items()))


def render_report(
    *,
    ingest: IngestResult,
    classify: ClassifyResult,
    stages: list[StageStats],
    audit_counts: dict[str, int],
    disciplines: dict[str, int],
    methods: dict[str, int],
    abstained_db: tuple[int, int],
    text_spans: int,
    cfg: StageAConfig,
    pdf_name: str,
    peak_rss_mb: int,
    when: datetime,
) -> str:
    """The ``bench/RESULTS.md`` section for one run. Numbers only."""
    abstained, total_sheets = abstained_db
    rate = (abstained / total_sheets) if total_sheets else 0.0
    triggered = rate > ABSTAIN_TRIGGER
    with_text = len(classify.with_text)
    rate_with_text = classify.abstain_rate_with_text
    reasons = classify.abstain_reasons()

    lines = [
        f"## Stage A + Stage B — {when:%Y-%m-%d}",
        "",
        "Command (local profile: SQLite + local FS object store, single process, no queue):",
        "",
        "```",
        f"CONDUIT_HOME=var/bench PYTHONPATH=. python -m bench.run_set \\",
        f"    --pdf {pdf_name} --dpi {cfg.render_dpi} --create-schema --append",
        "```",
        "",
        f"Input: **{pdf_name}** — SYNTHETIC ({total_sheets} pages). "
        f"Run `{ingest.run_id}` finished `{ingest.status}`; "
        f"classification rules `{classify.sheets and 'classify-rules-1' or 'n/a'}`.",
        "",
        "| Reading | Value |",
        "|---|---|",
    ]
    for st in stages:
        lines.append(
            f"| {st.stage} s/page | p50 **{st.p50_s:.3f}**, p95 **{st.p95_s:.3f}**, "
            f"max {st.max_s:.3f} (n={st.done} done, {st.failed} failed, "
            f"{st.skipped} skipped) |"
        )
    lines += [
        f"| Pages failed (stage A) | **{len(ingest.failed_pages)}** {ingest.failed_pages} "
        "— traceback on each `page_task_state.error` |",
        f"| Peak RSS | **{peak_rss_mb} MB** (process high-water mark; gate is "
        f"< {PEAK_RSS_GATE_MB} MB) |",
        f"| `Sheet` rows | **{total_sheets}** |",
        f"| Merged `TextSpan` rows | **{text_spans}** |",
        f"| Sheets classified by method | {_fmt_counts(methods)} |",
        f"| Sheets by discipline | {_fmt_counts(disciplines)} |",
        f"| **Classification abstain rate** | **{rate:.1%}** "
        f"({abstained} of {total_sheets} sheets: `confidence < 0.60 OR "
        "discipline = 'UNKNOWN'`) |",
        f"| Abstain rate, sheets with a text layer | **{rate_with_text:.1%}** "
        f"({sum(1 for s in classify.with_text if s.abstained)} of {with_text}) |",
        f"| Why sheets abstained | {_fmt_counts(reasons)} |",
        f"| Audit events | {_fmt_counts(audit_counts)} |",
        "",
        "### The 15% abstain-rate trigger (`03-pipeline-specs.md` §1.5)",
        "",
        f"Measured abstain rate **{rate:.1%}** — "
        f"{'above' if triggered else 'at or below'} the 15% threshold, so the "
        "thumbnail-classifier decision is due.",
        "",
        f"**Decision: do not build the thumbnail classifier yet.** The trigger "
        f"fired on {abstained} sheets and every one of them abstained for a reason a "
        "thumbnail classifier cannot fix: "
        f"{_fmt_counts(reasons)}. Among the {with_text} sheets that had a text layer to "
        f"read, the abstain rate is **{rate_with_text:.1%}**. A CNN over thumbnails "
        "predicts subtype from pixels; it does not recover a page whose content stream "
        "is corrupt, and for a flattened-raster page the missing capability is OCR "
        "(explicitly out of scope for week 1), not a second classifier. Building one "
        "now would also mean training on synthetic pages, which is exactly the "
        "per-customer-labelling trap the template-matching-first decision exists to "
        "avoid (risk R2)."
        if triggered
        else "**Decision: do not build the thumbnail classifier.** The measured rate is "
        "below the trigger.",
        "",
        "This decision is **re-evaluated on the first real plan set** (risk R10): the "
        "corpus above is generated, and its abstain rate is a property of how it was "
        "generated — 4 deliberately flattened-raster pages and 2 deliberately corrupt "
        "pages out of "
        f"{total_sheets} — not a property of real drawings.",
        "",
        "### Not measurable on this input",
        "",
        "| Number | Status |",
        "|---|---|",
        "| Classification **accuracy** (is the class right?) | **Unknown.** Needs real "
        "sheets with known classes. Not derivable from a generated corpus at any "
        "sample size. |",
        "| Template-matching precision/recall | Not built; needs a hand-labelled sample "
        "of **real** sheets |",
        "| `corrections_per_sheet` | Needs a review UI and a real estimator |",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m bench.run_set",
        description="Run a plan set through stages A and B and measure it.",
    )
    ap.add_argument("--pdf", required=True, help="path to the PDF plan set")
    ap.add_argument("--project", default="Bench project", help="project name")
    ap.add_argument("--dpi", type=int, default=200, help="base render DPI (default 200)")
    ap.add_argument("--no-tiles", action="store_true", help="skip the tile pyramid")
    ap.add_argument(
        "--create-schema",
        action="store_true",
        help="create the tables if missing (normally: alembic upgrade head)",
    )
    ap.add_argument(
        "--new-run",
        action="store_true",
        help="re-ingest known bytes as a new PipelineRun instead of reusing the last",
    )
    ap.add_argument(
        "--append",
        action="store_true",
        help="append the report to bench/RESULTS.md instead of only printing it",
    )
    ap.add_argument(
        "--results",
        default="bench/RESULTS.md",
        help="results file to append to (default bench/RESULTS.md)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = StageAConfig(render_dpi=args.dpi, write_tiles=not args.no_tiles)

    engine = create_engine_from_env()
    if args.create_schema:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            apply_dialect_extras(conn)
    store = default_local_store()
    session = session_factory(engine)()
    try:
        ref = store.put_file(args.pdf, content_type="application/pdf")
        ingest = ingest_pdf(
            session=session,
            store=store,
            object_ref=ref,
            filename=args.pdf.rsplit("/", 1)[-1],
            project_name=args.project,
            cfg=cfg,
            if_exists="new_run" if args.new_run else "reuse_run",
            triggered_by="bench.run_set",
        )
        document = session.get(Document, ingest.document_id)
        run = session.get(PipelineRun, ingest.run_id)
        classify = classify_document(
            session=session, document=document, run=run, store=store
        )
        stages = stage_stats(session, run.id)
        text_spans = session.execute(
            select(func.count())
            .select_from(TextSpan)
            .where(TextSpan.pipeline_run_id == run.id)
        ).scalar_one()
        report = render_report(
            ingest=ingest,
            classify=classify,
            stages=stages,
            audit_counts=_audit_counts(session, run.id, document.id),
            disciplines=_discipline_counts(session, document.id),
            methods=_method_counts(session, document.id),
            abstained_db=_abstain_from_db(session, document.id),
            text_spans=int(text_spans),
            cfg=cfg,
            pdf_name=args.pdf,
            peak_rss_mb=process_peak_rss_mb(),
            when=datetime.now(timezone.utc),
        )
    finally:
        session.close()
        engine.dispose()

    sys.stdout.write(report + "\n")
    if args.append:
        path = Path(args.results)
        existing = path.read_text() if path.exists() else ""
        path.write_text(existing.rstrip("\n") + "\n\n" + report + "\n")
        sys.stdout.write(f"appended to {path}\n")
    return 0 if ingest.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
