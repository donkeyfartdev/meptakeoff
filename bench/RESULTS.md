# bench/RESULTS.md — measured readings

**SYNTHETIC CORPUS — MEMORY AND THROUGHPUT ONLY. NO ACCURACY MEANING.**
Every number below was measured on `bench/make_corpus.py` output, which is
generated geometry, not MEP drawings. Detection recall, classification accuracy
and measurement error do **not** appear here and cannot be derived from this
input (risk R10 — real plan sets are still the top ask of the owner). Per the
roadmap rule, synthetic-derived accuracy numbers are deleted, not caveated.

## Stage A — 2026-08-11

Command (local profile: SQLite + local FS object store, single process, no queue):

```
CONDUIT_HOME=var/bench200 PYTHONPATH=. python -m conduit.ingest.run \
    --pdf <24-page synthetic corpus> --create-schema --dpi 200
```

Machine: 3.9 GB RAM, `/home` a 600 MB volume. Python 3.12, PyMuPDF 1.28.2,
Pillow 12.3.0, zstandard 0.25.0.

Run **completed** (`completed_with_errors`, which is the expected terminal state for
a corpus containing deliberately corrupt pages). Re-measured by the lead from the
`page_task_state` rows of the finished run, replacing an earlier partial table.

| Reading | Value |
|---|---|
| Pages processed | **24 of 24** — run status `completed_with_errors` |
| Pages failed | **2** (pages 9 and 17 — exactly the corpus's deliberately corrupt pages), each with a full traceback stored in `page_task_state.error`; the run still completed |
| Rasterise s/page | p50 **8.647**, p95 **13.862**, max **14.18** (n=22 succeeded) |
| Text-index s/page | p50 **0.005**, p95 **0.068**, max **0.07** |
| Total stage A wall time | **173.8 s** for 22 pages (rasterise 173.5 s + text index 0.3 s) |
| Peak RSS | **376 MB** (process high-water mark; gate is < 1500 MB) |
| Live page rasters per worker | **1** (enforced, `tests/test_ingest_memory.py`) |
| Bytes written to the object store | **5,523,563 B across 1,948 distinct objects** |
| `Sheet` rows | **24** (22 with a `tile_base_key`; the 2 corrupt pages have none) |
| Merged `TextSpan` rows | **432** |
| Audit events | 1 `document.ingested`, 1 `run.created`, 24 `sheet.ingested`, 1 `run.completed` |

Extrapolation, stated as arithmetic and not as a measurement: at p50 8.6 s/page,
a **200-sheet set is ~29 minutes single-process**. Nothing here says what two
workers on one box would do.

### What these numbers do and do not mean

* **Rasterise dominates by ~1000x.** Text extraction and the line merge are
  milliseconds; the pyramid is seconds. Tile count scales with dpi squared, so
  a `--dpi 72` pass is ~7x cheaper and is what the test suite uses.
* **Peak RSS is a process high-water mark**, not a per-page cost — `psutil` is
  not installed and pipeline code may not read `/proc`. See
  `conduit/ingest/metrics.py`. It is the honest reading available; the
  structural guarantee (one live raster) is tested instead of inferred.
* **Single process, no queue.** Nothing here says what happens with two arq
  workers on one box. Untested (`conduit/PROFILES.md`).
* No Postgres, no S3: SQLite write cost and local-FS `fsync` per object are in
  these timings and will not be the same numbers on the production profile.

### Not measured yet (and honest about it)

| Number | Status |
|---|---|
| Classification abstain rate | Stage B is not built (Slice C) |
| Template-matching precision/recall | Not built; needs a hand-labelled sample of **real** sheets |
| `corrections_per_sheet` | Needs a review UI and a real estimator |
| Anything on 200 real sheets | **Unknown — no real plan set exists yet (R10)** |
