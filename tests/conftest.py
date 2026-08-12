"""Shared fixtures.

The synthetic corpus is built ONCE per test session into pytest's tmp
directory, not committed: it is derived data, it takes about a second to make,
and ``/home`` on the development machine is a 300 MB volume.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conduit.bench.make_corpus import CorpusManifest, build

CORPUS_PAGES = 24


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, CorpusManifest]:
    out = tmp_path_factory.mktemp("corpus") / "synthetic_corpus.pdf"
    manifest = build(out, CORPUS_PAGES)
    return out, manifest


@pytest.fixture(scope="session")
def corpus_bytes(corpus: tuple[Path, CorpusManifest]) -> bytes:
    return corpus[0].read_bytes()


@pytest.fixture(scope="session")
def manifest(corpus: tuple[Path, CorpusManifest]) -> CorpusManifest:
    return corpus[1]


@pytest.fixture
def backend(corpus_bytes: bytes):
    from conduit.pdf.pymupdf_backend import PyMuPdfBackend

    with PyMuPdfBackend(corpus_bytes, filename_hint="synthetic_corpus.pdf") as be:
        yield be


@pytest.fixture
def store(tmp_path: Path):
    from conduit.store import LocalFsStore

    return LocalFsStore(tmp_path / "objects")


# ---------------------------------------------------------------------------
# Stage A
# ---------------------------------------------------------------------------
#
# Stage A is exercised end to end once per session, and everything it wrote is
# then queried by the tests. Two deliberate reductions, so a test run stays
# cheap on a 600 MB volume with a 3.9 GB box:
#
#   * 8 pages, not 24 — still ARCH D and ARCH E, still all four /Rotate values,
#     still one flattened-raster page and (always) two corrupt pages;
#   * 72 dpi, not the production 200 — tile count scales with dpi squared, so
#     this is ~7x less work. The *geometry* under test is identical; the
#     throughput and memory numbers that matter are measured at 200 dpi by
#     `python -m bench.run_stage_a` and written to bench/RESULTS.md, not here.
#
# The object store is deleted at session teardown: tiles are derived data and
# nothing downstream of the tests wants them.

INGEST_CORPUS_PAGES = 8
INGEST_DPI = 72


@pytest.fixture(scope="session")
def small_corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, CorpusManifest]:
    out = tmp_path_factory.mktemp("corpus8") / "synthetic_corpus.pdf"
    return out, build(out, INGEST_CORPUS_PAGES)


@pytest.fixture(scope="session")
def stage_a_env(tmp_path_factory: pytest.TempPathFactory, small_corpus):
    """A throwaway SQLite database + object store, schema created."""
    import shutil
    from types import SimpleNamespace

    from conduit.db.models import Base
    from conduit.db.session import (
        apply_dialect_extras,
        create_engine_from_env,
        session_factory,
    )
    from conduit.store import LocalFsStore

    root = tmp_path_factory.mktemp("stage_a")
    engine = create_engine_from_env(f"sqlite+pysqlite:///{(root / 'conduit.db').as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        apply_dialect_extras(conn)
    store = LocalFsStore(root / "objects")

    env = SimpleNamespace(
        engine=engine,
        Session=session_factory(engine),
        store=store,
        root=root,
        pdf=small_corpus[0],
        manifest=small_corpus[1],
        pdf_bytes=small_corpus[0].read_bytes(),
    )
    yield env
    engine.dispose()
    shutil.rmtree(root / "objects", ignore_errors=True)


@pytest.fixture(scope="session")
def ingested(stage_a_env):
    """One stage A run over the 8-page corpus. Shared by the ingest tests."""
    from conduit.ingest import StageAConfig, ingest_pdf
    from conduit.ingest.render import peak_live_pixmaps, reset_pixmap_counters

    cfg = StageAConfig(render_dpi=INGEST_DPI)
    reset_pixmap_counters()
    session = stage_a_env.Session()
    try:
        result = ingest_pdf(
            session=session,
            store=stage_a_env.store,
            chunks=[stage_a_env.pdf_bytes],
            filename="synthetic_corpus.pdf",
            project_name="stage-a-tests",
            cfg=cfg,
            triggered_by="pytest",
        )
    finally:
        session.close()
    stage_a_env.result = result
    stage_a_env.cfg = cfg
    stage_a_env.peak_live_pixmaps = peak_live_pixmaps()
    return stage_a_env


# ---------------------------------------------------------------------------
# Stage B
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def classified(ingested):
    """Stage B over the same 8-page run stage A produced. Session-scoped.

    Stage B is cheap (no rendering), but it reads the rows stage A wrote, so it
    runs against the same database rather than a second ingest.
    """
    from conduit.classify import classify_document
    from conduit.db.models import Document, PipelineRun

    session = ingested.Session()
    try:
        document = session.get(Document, ingested.result.document_id)
        run = session.get(PipelineRun, ingested.result.run_id)
        result = classify_document(
            session=session, document=document, run=run, store=ingested.store
        )
    finally:
        session.close()
    ingested.classify = result
    return ingested
