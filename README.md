# Conduit Takeoff — Week 1, Slices A + B

Auditable MEP quantity takeoff from multi-sheet construction PDF plan sets.
The engineering design package lives in `../design/`; this repo is the code it
describes, starting from the seams.

**What exists after Slice A:** the package layout, the copied data models with
Alembic migrations, the storage-profile seams (SQLite + local FS now, Postgres
+ S3 later), the `PdfBackend` protocol with a PyMuPDF implementation, a
synthetic test corpus, and tests that assert real properties of all of it.

**What exists after Slice B — stage A, end to end.** One command takes a PDF
and produces auditable rows and artifacts:

1. the upload is streamed into the object store with its **sha256 computed in
   flight** (never buffered, hashed and written twice) — the key *is* the
   digest, so re-ingesting the same bytes stores nothing new;
2. page count, MediaBox and `/Rotate` are read **without rendering**, and every
   page gets a `Sheet` row carrying the full pdf_points <-> raster_px
   transform — including the corrupt pages, so a failure is attributable to a
   page rather than being a hole in the sequence;
3. `Document` + `PipelineRun(queued -> running -> completed |
   completed_with_errors)` are written through the real ORM;
4. each page is rasterised at 200 dpi into a **WebP deep-zoom pyramid** built
   from `render_page(clip=...)` sub-renders, plus a thumbnail and (under a
   pixel budget) a whole-page PNG. **No more than one page raster is live per
   worker** — enforced by `conduit/ingest/render.py` and asserted in
   `tests/test_ingest_memory.py`;
5. vector paths are cached as `paths.json` compressed with zstd (gzip
   fallback, recorded);
6. text spans are merged into logical lines per `03-pipeline-specs.md` §2.2 and
   persisted as `TextSpan` rows with `normalized_text`;
7. a `PageTaskState` row per page per stage carries `duration_ms` and
   `peak_rss_mb`; a failed page stores its traceback and **the run still
   completes**.

**What does not exist:** stage B (classification), the arq job queue, the
FastAPI app, any detector, any UI, OCR, exports. See `conduit/PROFILES.md` for
the honest list of what is untested on this machine, and `bench/RESULTS.md`
for the measured stage A numbers.

## Setup

Python 3.11+. Dependencies: `pydantic`, `SQLAlchemy`, `alembic`, `pymupdf`,
`pytest`. On the shared development machine, reuse the existing virtualenv
rather than creating another one (`/home` is a 600 MB volume — check
`df -h /home` before installing anything):

```bash
VENV=../design/.venv/bin          # or: python -m venv .venv && . .venv/bin/activate
$VENV/pip3 install --no-cache-dir pymupdf pytest alembic
```

`pytest-xdist` is deliberately **not** installed. If you add it, cap it at
`pytest -n 2` — never `-n auto`.

## Run everything

All commands are run from the repo root, with the repo root on `PYTHONPATH`
(or after `pip install -e .`).

```bash
# 1. tests
PYTHONPATH=. $VENV/python -m pytest -q

# 2. migrations, on the local SQLite profile
PYTHONPATH=. $VENV/alembic upgrade head
PYTHONPATH=. $VENV/alembic downgrade base
PYTHONPATH=. $VENV/alembic upgrade head
PYTHONPATH=. $VENV/alembic check          # models and migrations agree

# 3. stage A over a plan set (writes to $CONDUIT_HOME, default ./var)
CONDUIT_HOME=var/run1 PYTHONPATH=. $VENV/python -m conduit.ingest.run \
    --pdf bench/out/synthetic_corpus.pdf --create-schema --dpi 200
#   --new-run   re-run over bytes already ingested (blobs dedupe)
#   --no-tiles  text/paths only, skips the expensive pyramid
#   --dpi 72    ~7x fewer tiles, for a quick pass

# 4. the synthetic corpus (24 pages by default)
PYTHONPATH=. $VENV/python -m bench.make_corpus
PYTHONPATH=. $VENV/python -m bench.make_corpus --pages 48 --out /tmp/big.pdf

# 5. the design package's own model checker, still passing on the copies
cd ../design/models && ../.venv/bin/python verify_models.py   # ALL CHECKS PASSED
```

Reading a corpus back through the backend, which is what `python -m
bench.make_corpus` output is for:

```python
from conduit.pdf.pymupdf_backend import PyMuPdfBackend

with PyMuPdfBackend(open("bench/out/synthetic_corpus.pdf", "rb").read()) as be:
    for page in range(1, be.document_info().page_count + 1):
        g = be.page_geometry(page, dpi=200)
        print(page, g.rotation_deg, f"{g.width_pt:.0f}x{g.height_pt:.0f}pt",
              f"{g.width_px}x{g.height_px}px")
```

## Layout

```
conduit/
  db/models.py        VERBATIM copy of design/models/orm.py — never edit here
  schemas.py          VERBATIM copy of design/models/schemas.py
  db/session.py       engine/session; the ONLY place dialect differences live
  store/              ObjectStore protocol, LocalFsStore, documented S3 seam
  pdf/backend.py      PdfBackend protocol — no PDF library, no PDF types
  pdf/pymupdf_backend.py   the only module that imports PyMuPDF
  geometry.py         pdf_points <-> raster_px, all four rotations
  errors.py           typed page-level vs document-level failures
  ingest/             stage A: config, render budget, metrics, tiles,
                      paths_dump, textlines, stage_a, run (CLI)
  classify/           stage B — not built yet
  bench/make_corpus.py     synthetic corpus generator
  PROFILES.md         local vs production profile, and what is untested
alembic/              initial migration (16 tables + Postgres-only extras)
bench/                CORPUS.md + generated corpora (git-ignored)
tests/
```

## The two rules this repo enforces mechanically

1. **No PyMuPDF type crosses `conduit/pdf/backend.py`** — PyMuPDF is AGPL
   (risk R9) and the licence decision is the owner's; engineering's job is to
   keep the swap to `pypdfium2` cheap. `tests/test_pdf_backend_contract.py`
   resolves every annotation on every protocol method and fails if one comes
   from a PDF library, and asserts only two modules in the tree import one.
2. **No pipeline module opens a filesystem path** — everything goes through
   `ObjectStore`, so the local profile is a faithful rehearsal of S3.
   `tests/test_no_direct_paths.py` scans the package and lists its exceptions.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CONDUIT_DATABASE_URL` | `sqlite+pysqlite:///<CONDUIT_HOME>/conduit.db` | any SQLAlchemy URL |
| `CONDUIT_HOME` | `./var` | local-profile state directory |
| `CONDUIT_PDF_BACKEND` | `pymupdf` | selects the `PdfBackend` implementation |
| `CONDUIT_MUPDF_STDERR` | unset | re-enable MuPDF's raw stderr chatter |

No absolute paths are baked into any config file: the tree moves to a real git
repo without edits.

## Honesty notes

* The only test input is synthetic (`bench/CORPUS.md`). No accuracy number of
  any kind has been produced, and none may be derived from that file.
* Peak RSS and throughput for stage A **are** measured, on the synthetic
  corpus, in `bench/RESULTS.md`. They are memory and throughput readings only;
  no accuracy number exists or can exist from that input.
* Postgres, Redis, MinIO and any detector are not installed and not exercised
  here; `conduit/PROFILES.md` lists precisely what that leaves untested.
