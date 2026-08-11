# Runtime profiles — what is real here, and what is not

There are two profiles. The code is the same; the components under it are not.

| | **Local profile** (default, what this repo is tested on) | **Production profile** (`02-tech-stack.md`) |
|---|---|---|
| Database | SQLite file (`var/conduit.db`) | PostgreSQL 16 |
| Object store | `LocalFsStore` — a directory, content-addressed by sha256 | S3-compatible (MinIO / SeaweedFS / Garage / AWS S3) |
| Queue | none — everything runs in-process | Redis + arq, three queues |
| PDF backend | PyMuPDF via `PdfBackend` | same, pending the AGPL decision (risk R9) |
| Detector | none installed (no torch, no ultralytics, no OpenCV) | template matching now, ONNX-served YOLO later |
| Selected by | nothing to set | `CONDUIT_DATABASE_URL`, `CONDUIT_S3_*` |

The local profile exists because the development machine has a **600 MB `/home` volume**
(300 MB when this was written). Postgres, Redis and MinIO container images together exceed the entire
disk, so they are not run here — not "not yet configured", not run at all.

## Exactly what is untested on the local profile

Stated plainly, because an untested thing that is not written down becomes a
thing someone assumes works.

1. **The `pg_trgm` GIN index** on `text_span.normalized_text`. Declared in
   `conduit/db/models.py` and created by the initial migration **on Postgres
   only**; skipped on SQLite by `apply_dialect_extras()`. No fuzzy-text query
   has been run against it. Locally, any such search degrades to a full scan.
2. **The `audit_event_seq_seq` sequence.** Created on Postgres only. On SQLite,
   `next_audit_seq()` emulates it with `SELECT max(seq) + 1`, which is **not
   concurrency-safe** and is only sound because the local profile has a single
   writer. Never let this emulation reach production.
3. **JSONB semantics.** `JSON().with_variant(JSONB(), "postgresql")` means
   SQLite stores JSON text. Containment (`@>`), JSONB indexes and their query
   plans are untested here.
4. **Alembic on Postgres.** The migration cycle (`upgrade head` → `downgrade
   base` → `upgrade head`) is verified on SQLite only. Two things are known to
   need attention when a Postgres instance exists:
   - migrations run with `PRAGMA foreign_keys=OFF` on SQLite because the schema
     contains mutually-dependent foreign keys (`measurement` ↔ `review_action`
     ↔ `sheet_scale`) with no valid single drop order. On Postgres the
     `downgrade` path will likely need `DROP TABLE ... CASCADE` or explicit
     constraint drops. **Untested.**
   - `render_as_batch` is enabled for SQLite only; it is a no-op on Postgres.
5. **The S3/MinIO object store.** `conduit/store/s3.py` is a documented seam
   that raises `NotImplementedError`. Nothing about multipart upload, staging
   keys or `ClientError` translation has been executed.
6. **Concurrency of any kind.** No worker pool, no queue (arq), no parallel
   page processing. Stage A peak RSS and throughput **have** now been measured
   single-process at 200 dpi — see `bench/RESULTS.md` — but nothing is known
   about two workers sharing this box.
7. **Server-side `func.now()` defaults.** They resolve to
   `CURRENT_TIMESTAMP` on SQLite, which returns a naive UTC value on read-back.
   The application relies on the Python-side `default=utcnow`.
8. **Pages whose CropBox differs from their MediaBox.** `page_geometry` raises
   `PageGeometryError` for these rather than guessing; the synthetic corpus
   contains none, so the *handling* is untested against a real cropped sheet.
9. **Everything about accuracy.** There are no real plan sets (risk R10). The
   only test input is the synthetic corpus — see `bench/CORPUS.md`.

## Moving to the production profile

```bash
export CONDUIT_DATABASE_URL='postgresql+psycopg://conduit:***@localhost:5432/conduit'
alembic upgrade head          # creates the 16 tables + the Postgres-only extras
```

Nothing else in the tree changes: no absolute paths are baked into
`alembic.ini`, `pyproject.toml` or any module.


## Stage A deviations, recorded rather than silent

1. **`paths.json.zst` really is zstd here** (`zstandard` 0.25.0 is installed).
   If it is ever absent the codec falls back to **gzip**; the codec is written
   into the artifact itself, into `PipelineRun.model_versions["paths_codec"]`
   and into each page's audit payload, so no consumer has to guess. It is never
   a silent format change.
2. **No `{z}/{x}/{y}` tile paths.** The object store is content-addressed, so
   each page gets a **tile manifest** (level geometry + object key per tile) and
   `Sheet.tile_base_key` holds that manifest's key. Blank tiles dedupe to one
   object for free.
3. **`paths_object_key` has nowhere to live in the schema.** `Sheet` has
   columns for the raster, thumbnail and tile base, but not for the cached path
   dump, so stage A records it in the per-page `AuditEvent("sheet.ingested")`
   payload. That is traceable but not queryable; adding `Sheet.paths_object_key`
   is a week-2 schema question (it would mean editing the verbatim model copies
   and a new migration).
4. **Whole-page PNG only under 40 Mpx.** An ARCH E sheet at 200 dpi is 69 Mpx =
   ~207 MB of samples, which contradicts the memory discipline this stage
   exists to keep, so `Sheet.raster_object_key` is left NULL for such pages and
   the max-zoom tile level is the authoritative raster. Counted per run in
   `bench/RESULTS.md`.
5. **The PDF is opened from memory.** `PyMuPdfBackend` takes bytes, so a 400 MB
   plan set is resident while it is parsed (the *upload* is streamed and hashed
   in flight; the parse is not). Fine for a 24-page corpus, not measured for a
   real set.
6. **Pyramid levels below 36 dpi are downsampled, not re-rendered.** The
   backend refuses < 36 dpi, so coarse levels and the thumbnail are resampled
   from one 36 dpi page render. Those levels are resampled pixels rather than
   re-rendered vector art, and each level says which it is in the manifest.
7. **One documented change to the §2.2 line merge**: spans whose font sizes
   differ by more than 10% do not merge. Without it, two different title-block
   fields on a shared baseline merged into one line. `MERGE_VERSION` moved to
   `linemerge-2` when this was added.
