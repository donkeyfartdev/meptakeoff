# AGENTS.md — conventions for anyone working in this repo

Read this before touching code. These are the things that are expensive to
rediscover, and a few that are enforced by tests rather than by goodwill.

The engineering design package (architecture, pipeline specs, roadmap, risk
register, the canonical data models) is **not** in this repo — it lives in
`../design/` on the shared machine. Measured engineering readings live in
`bench/RESULTS.md`. Neither is a place to put aspirations.

## 1. Environment

- `/home` is a **600 MB volume**. Run `df -h /home` before installing anything,
  and use `--no-cache-dir`. A failed write here is a full disk, not a fault.
- **Reuse the shared virtualenv `/home/team/shared/design/.venv`. Never create a
  second one.** Its binaries are `python`, `pip3`, `pytest`, `alembic` — there
  is no `pip`. Typical invocation from the repo root:

  ```bash
  VENV=/home/team/shared/design/.venv/bin
  PYTHONPATH=. $VENV/python -m pytest -q
  ```
- `pyproject.toml` already sets `addopts = "-q"`, so passing `-q` yourself makes
  it `-qq` and pytest **prints no `N passed` summary line** — just progress dots
  and exit status. Run plain `python -m pytest` when you want the count.
- `pytest-xdist` is deliberately **absent**. If you ever add it, cap it at
  `pytest -n 2` — never `-n auto`; this machine has modest RAM and the ingest
  tests hold page rasters.

## 2. The local profile, and why

There is **no Docker** on this machine — the Postgres/Redis/MinIO images alone
exceed the whole volume. So everything runs on the **local profile: SQLite +
local-filesystem object store**, behind explicit seams:

| Seam | Local now | Production later |
|---|---|---|
| `conduit/db/session.py` | SQLite (`CONDUIT_DATABASE_URL`) | Postgres |
| `conduit/store/` (`ObjectStore`) | `LocalFsStore` | S3/MinIO (`store/s3.py`) |
| `conduit/pdf/backend.py` (`PdfBackend`) | PyMuPDF | pypdfium2, if AGPL forces it |

"It works" therefore carries an asterisk until it runs against real Postgres and
a real S3-compatible store. `conduit/PROFILES.md` records exactly what that
leaves untested — **keep it current**; if you add something the local profile
cannot exercise, it goes in that file in the same commit.

## 3. Rules the tests enforce

- **No PDF library outside `conduit/pdf/pymupdf_backend.py` and `bench/`
  tooling** (`conduit/bench/make_corpus.py`). No PyMuPDF type may appear in a
  `PdfBackend` annotation. Enforced by `tests/test_pdf_backend_contract.py`.
  PyMuPDF is AGPL (risk R9) and the licence call is the owner's; engineering's
  job is to keep the swap cheap.
- **No pipeline module opens a filesystem path.** Everything goes through
  `ObjectStore`, so the local profile is a faithful rehearsal of S3. Enforced by
  `tests/test_no_direct_paths.py`, which carries an explicit `ALLOWED` list —
  adding to that list is a design decision, not a fix.
- `conduit/db/models.py` and `conduit/schemas.py` are **verbatim copies** of
  `../design/models/orm.py` and `schemas.py`. Never edit them here; change the
  design package, re-run its `verify_models.py`, then re-copy.
  `tests/test_models_copy.py` compares them.

## 4. Schema changes

New behaviour that needs a column gets a **new, additive Alembic migration**.
Never edit a migration that already exists — including
`alembic/versions/20260811_0147_initial_schema.py`. Prefer nullable columns and
backfills over destructive rewrites. After any schema change, the round trip
must still be clean:

```bash
PYTHONPATH=. $VENV/alembic upgrade head && \
PYTHONPATH=. $VENV/alembic downgrade base && \
PYTHONPATH=. $VENV/alembic upgrade head && \
PYTHONPATH=. $VENV/alembic check
```

## 5. Provenance is not optional

Every persisted quantity or interpretation must be traceable to a sheet id, page
number, bounding box, and the extractor/model version that produced it. A change
that writes a number with no such path is rejected regardless of how well it
works. Provenance that can only be reconstructed from inside a JSON audit
payload is not queryable provenance — if a reviewer needs it, give it a column.

Evidence is immutable; interpretation is versioned. Never mutate an evidence row
to "fix" it — supersede it with a new interpretation.

## 5a. Material names are generated, never typed

The estimator-facing output has to **join** to a pricebook we do not own, so no
material name is ever free text. Every name comes out of
`conduit/materials/` — `render_item_name()` over a `Material`, an `ItemType`
and a `Size` — and every size has exactly one display form and one key, where
the key is *defined* as `normalize_text(display)`, the same normaliser stage A
uses for `TextSpan.normalized_text`.

If you need a word the vocabulary does not have, **add it to the registry**;
do not write the string at the call site. An unresolvable name is a review
item, never a new line item.

Electrical and HVAC wording is *mostly* still unconfirmed and lives in
`conduit/materials/proposed.py` — that whole file is `PROPOSED_PENDING_OWNER`
and is meant to be replaced wholesale, so **never leave a confirmed word in
it**. The words the owner has confirmed live in `vocabulary.py`
(`ELECTRICAL_ITEM_TYPES`, `CONDUIT_BODY_FAMILY`, and the shared `COUPLING`
entry). Promoting a word requires an owner answer and an edit to
`tests/test_materials_vocabulary.py`, which asserts the exact confirmed set.

Two rules that vocabulary now enforces and that are easy to break by accident:

- **The unit is a property of `ItemCategory`, never of a line.** Duct is
  `LB` because `ItemCategory.DUCT` maps to `POUNDS` (owner-directed), flex duct
  is `LF` because it is a different category. If you find yourself choosing a
  unit at a call site, the category is wrong.
- **A family word is not a line item, and an ambiguous word is not resolved.**
  `condulet` resolves to a `Family` and can never open an aggregation key; `LB`
  means both a conduit body and a pound, so `resolve_term()` /
  `resolve_item_type()` / `resolve_unit()` raise `AmbiguousTerm` without a
  `discipline=` or `prefer=`. Do not add a "sensible default" to either.
- **`None` from a resolver means "unknown word" and nothing else.** A word the
  vocabulary *does* know but cannot turn into one line raises — `condulet`
  through `resolve_item_type()` raises `UnderSpecifiedTerm`. Returning `None`
  for a known word is how a real trade term becomes an `AttributeError` on
  `.code` three call sites away. New family or class words inherit this;
  `test_no_word_this_vocabulary_knows_returns_none_from_resolve_item_type`
  enforces it for every registered spelling.

The specification is `docs/output-schema.md`; derived-quantity rules and the
counted-versus-factored distinction are in `docs/derived-quantities.md`. Both
are verified against the ORM with the team's `verify-docs-against-models`
skill — run it after editing either.

## 6. Honesty about numbers

- The only input in this repo is **synthetic** (`bench/CORPUS.md`,
  reproduced by `python -m bench.make_corpus`). Memory and throughput readings
  from it are real. **Accuracy readings from it do not exist.**
- Therefore: **no accuracy claim derived from synthetic data anywhere** — not in
  docs, not in docstrings, not in `bench/RESULTS.md`, and not in commit
  messages. Numbers get labelled with what they were measured on, or deleted.
- Commit messages state what was measured, not what was hoped.

## 7. Nothing generated is tracked

Runtime output goes under `var/` (`CONDUIT_HOME`) and stays gitignored. So do
SQLite files, tiles and page rasters, `__pycache__`, caches, venvs, and
`bench/out/` — **the corpus generator is source, its output is not.** Check with
`git status --porcelain` before committing; if something generated is already
staged, unstage it rather than committing and reverting later.

## 8. Workflow

Every slice lands as a **pull request** on a branch named for it
(`week1/slice-c-classify`, `week2/stage-c-text-index`). Never commit to `main`.
Leave the default branch checked out and the tree clean when you finish — every
delegation shares this one working tree. The team's standing process is
`/home/team/shared/WORKFLOW.md`.
