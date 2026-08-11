"""Stage A — ingest.

    PDF -> object store (sha256 in flight)
        -> Document / PipelineRun / Sheet rows
        -> per page: geometry without rendering, WebP tile pyramid + thumbnail,
           optional whole-page raster, paths.json.zst, merged TextSpan rows
        -> PageTaskState per page per stage with duration_ms and peak_rss_mb

Modules, and the one idea each is responsible for:

* ``config``      — every knob, with the reason for its default.
* ``render``      — the *enforced* one-page-raster-per-worker budget (risk R8).
* ``metrics``     — duration and RSS readings, and an honest note about what
  ``peak_rss_mb`` actually measures without ``psutil``.
* ``tiles``       — the deep-zoom pyramid from ``render_page(clip=...)``
  sub-renders, plus the tile manifest that replaces a ``{z}/{x}/{y}`` path in
  a content-addressed store.
* ``paths_dump``  — ``page_{n}.paths.json.zst`` (gzip fallback, recorded).
* ``textlines``   — the span->line merge of ``03-pipeline-specs.md`` §2.2 and
  the ``normalized_text`` canonicaliser.
* ``stage_a``     — the orchestrator: rows, audit events, per-page failure
  isolation, run status.
* ``run``         — ``python -m conduit.ingest.run --pdf <path>``.

Stage B (classification), the arq queue and the API are later slices; nothing
here imports them.
"""

from conduit.ingest.config import StageAConfig
from conduit.ingest.stage_a import IngestResult, PageReport, ingest_pdf

__all__ = ["IngestResult", "PageReport", "StageAConfig", "ingest_pdf"]
