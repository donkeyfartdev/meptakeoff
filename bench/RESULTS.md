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

## Stage A + Stage B — 2026-08-12

Command (local profile: SQLite + local FS object store, single process, no queue):

```
CONDUIT_HOME=var/bench PYTHONPATH=. python -m bench.run_set \
    --pdf bench/out/synthetic_corpus.pdf --dpi 200 --create-schema --append
```

Input: **bench/out/synthetic_corpus.pdf** — SYNTHETIC (24 pages). Run `9e9da9fa-3ad0-4262-86b9-ef2a6adfc31d` finished `completed_with_errors`; classification rules `classify-rules-1`.

| Reading | Value |
|---|---|
| rasterise s/page | p50 **8.705**, p95 **13.790**, max 14.180 (n=22 done, 2 failed, 0 skipped) |
| text_index s/page | p50 **0.005**, p95 **0.069**, max 0.069 (n=22 done, 2 failed, 0 skipped) |
| classify s/page | p50 **0.001**, p95 **0.002**, max 0.005 (n=22 done, 0 failed, 2 skipped) |
| Pages failed (stage A) | **2** [9, 17] — traceback on each `page_task_state.error` |
| Peak RSS | **376 MB** (process high-water mark; gate is < 1500 MB) |
| `Sheet` rows | **24** |
| Merged `TextSpan` rows | **360** |
| Sheets classified by method | `default_fallback` 6, `sheet_number_regex` 18 |
| Sheets by discipline | `E` 6, `M` 8, `P` 4, `UNKNOWN` 6 |
| **Classification abstain rate** | **25.0%** (6 of 24 sheets: `confidence < 0.60 OR discipline = 'UNKNOWN'`) |
| Abstain rate, sheets with a text layer | **0.0%** (0 of 18) |
| Why sheets abstained | `no_vector_text` 4, `page_failed_in_stage_a` 2 |
| Audit events | `document.ingested` 1, `run.classified` 1, `run.completed` 1, `run.created` 1, `sheet.classified` 22, `sheet.classify_skipped` 2, `sheet.ingest_failed` 2, `sheet.ingested` 22 |

### The 15% abstain-rate trigger (`03-pipeline-specs.md` §1.5)

Measured abstain rate **25.0%** — above the 15% threshold, so the thumbnail-classifier decision is due.

**Decision: do not build the thumbnail classifier yet.** The trigger fired on 6 sheets and every one of them abstained for a reason a thumbnail classifier cannot fix: `no_vector_text` 4, `page_failed_in_stage_a` 2. Among the 18 sheets that had a text layer to read, the abstain rate is **0.0%**. A CNN over thumbnails predicts subtype from pixels; it does not recover a page whose content stream is corrupt, and for a flattened-raster page the missing capability is OCR (explicitly out of scope for week 1), not a second classifier. Building one now would also mean training on synthetic pages, which is exactly the per-customer-labelling trap the template-matching-first decision exists to avoid (risk R2).

This decision is **re-evaluated on the first real plan set** (risk R10): the corpus above is generated, and its abstain rate is a property of how it was generated — 4 deliberately flattened-raster pages and 2 deliberately corrupt pages out of 24 — not a property of real drawings.

### Not measurable on this input

| Number | Status |
|---|---|
| Classification **accuracy** (is the class right?) | **Unknown.** Needs real sheets with known classes. Not derivable from a generated corpus at any sample size. |
| Template-matching precision/recall | Not built; needs a hand-labelled sample of **real** sheets |
| `corrections_per_sheet` | Needs a review UI and a real estimator |

### Delta against the Stage A table above

Both runs are 200 dpi on a 24-page synthetic corpus, single process, local
profile. The **2026-08-11 Stage A** table was read from that run's
`page_task_state` rows; the **2026-08-12 Stage A + Stage B** table above is a
separate run of `bench/run_set.py` on a *regenerated* corpus (the title-block
rotation fix in this slice changes the generated PDF, so it is not the same
bytes). Neither table is corrected from the other — they are two runs.

| Reading | 2026-08-11 (Stage A run) | 2026-08-12 (Stage A + B run) | Delta |
|---|---|---|---|
| Rasterise p50 s/page | 8.647 | 8.705 | +0.058 |
| Rasterise p95 s/page | 13.862 | 13.790 | −0.072 |
| Rasterise max s/page | 14.18 | 14.180 | 0 |
| Text-index p50 s/page | 0.005 | 0.005 | 0 |
| Text-index p95 s/page | 0.068 | 0.069 | +0.001 |
| Peak RSS | 376 MB | 376 MB | 0 |
| Pages failed | 2 (9, 17) | 2 (9, 17) | 0 |
| Merged `TextSpan` rows | 432 | 360 | **−72** |

The timing deltas are run-to-run noise at n=22 and are not a change in
behaviour. The **`TextSpan` delta is expected and is the corpus fix, not a
regression**: the old generator drew the title block at the page-space
bottom-right regardless of `/Rotate`, and the fix draws it in rotated display
space, which changes how many raw spans merge into a line on the rotated
sheets. The old corpus is not reproducible from this branch.
