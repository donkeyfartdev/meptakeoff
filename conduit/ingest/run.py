"""Stage A entry point.

    PYTHONPATH=. python -m conduit.ingest.run --pdf bench/out/synthetic_corpus.pdf

This is the *local profile* driver: SQLite + a directory object store, one
process, no queue. In the production profile the same ``ingest_pdf`` call is
made by an arq task whose bytes arrive from an HTTP upload; that is why the
library takes a stream (or an already-stored object) and never a path.

The upload path is streamed and hashed in flight — ``LocalFsStore.put_file``
reads 256 KiB at a time, updates the sha256 as it goes and ``os.replace``s the
temp file into its content-addressed key. A 400 MB plan set never sits in
memory here (it does when the PDF is later opened for parsing — see
``conduit/PROFILES.md``, "known limits").
"""

from __future__ import annotations

import argparse
import sys

from conduit.db.models import Base
from conduit.db.session import apply_dialect_extras, create_engine_from_env, session_factory
from conduit.ingest.config import StageAConfig
from conduit.ingest.stage_a import IngestResult, ingest_pdf
from conduit.store.local import default_local_store


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m conduit.ingest.run",
        description="Stage A: ingest a PDF plan set (store, geometry, tiles, paths, text).",
    )
    ap.add_argument("--pdf", required=True, help="path to the PDF to ingest")
    ap.add_argument("--project", default="Synthetic project", help="project name")
    ap.add_argument("--name", default=None, help="document name (defaults to the filename)")
    ap.add_argument("--dpi", type=int, default=200, help="base render DPI (default 200)")
    ap.add_argument("--tile-size", type=int, default=256, help="tile edge in px (default 256)")
    ap.add_argument("--no-tiles", action="store_true", help="skip the tile pyramid")
    ap.add_argument(
        "--new-run",
        action="store_true",
        help="re-ingest known bytes as a new PipelineRun instead of reusing the last one",
    )
    ap.add_argument(
        "--create-schema",
        action="store_true",
        help="create the tables if they are missing (normally: alembic upgrade head)",
    )
    ap.add_argument("--triggered-by", default="cli", help="recorded on the run and audit rows")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = StageAConfig(
        render_dpi=args.dpi,
        tile_size=args.tile_size,
        write_tiles=not args.no_tiles,
    )
    engine = create_engine_from_env()
    if args.create_schema:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            apply_dialect_extras(conn)

    store = default_local_store()
    filename = args.pdf.rsplit("/", 1)[-1]
    session = session_factory(engine)()
    try:
        ref = store.put_file(args.pdf, content_type="application/pdf")
        result = ingest_pdf(
            session=session,
            store=store,
            object_ref=ref,
            filename=filename,
            project_name=args.project,
            document_name=args.name,
            cfg=cfg,
            if_exists="new_run" if args.new_run else "reuse_run",
            triggered_by=args.triggered_by,
        )
    finally:
        session.close()
        engine.dispose()

    _print_summary(result, cfg)
    return 0 if result.status != "failed" else 1


def _print_summary(result: IngestResult, cfg: StageAConfig) -> None:
    out = sys.stdout
    if result.reused:
        out.write(
            f"already ingested: document {result.document_id} sha256 {result.sha256[:12]}…\n"
            f"  reusing run {result.run_id} ({result.status}); nothing re-rendered.\n"
            "  pass --new-run to force a new PipelineRun over the same bytes.\n"
        )
        return

    durations = sorted(p.duration_ms for p in result.pages)
    out.write(
        f"run {result.run_id} -> {result.status}\n"
        f"  document        {result.document_id}\n"
        f"  sha256          {result.sha256}\n"
        f"  object key      {result.object_key}\n"
        f"  pages           {result.page_count} "
        f"({len(result.ok_pages)} ok, {len(result.failed_pages)} failed"
        f"{': ' + str(result.failed_pages) if result.failed_pages else ''})\n"
        f"  render dpi      {cfg.render_dpi}\n"
        f"  tiles written   {result.tile_count}\n"
        f"  text spans      {result.text_span_count} (merged lines)\n"
        f"  paths codec     {result.paths_codec}\n"
        f"  bytes to store  {result.bytes_written}\n"
        f"  wall clock      {result.duration_ms / 1000:.1f} s"
        f" ({_p(durations, 50) / 1000:.2f} s/page p50,"
        f" {_p(durations, 95) / 1000:.2f} s/page p95)\n"
        f"  peak RSS        {result.peak_rss_mb} MB (process high-water mark)\n"
        "  SYNTHETIC INPUT WARNING: memory and throughput numbers from a\n"
        "  generated corpus are real; accuracy numbers from it do not exist.\n"
    )


def _p(sorted_values: list[int], pct: int) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round((pct / 100.0) * len(sorted_values))) - 1))
    return float(sorted_values[idx])


if __name__ == "__main__":
    sys.exit(main())
