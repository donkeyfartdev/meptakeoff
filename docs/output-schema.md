# Output schema — the estimator-facing material list

**Status:** specification. Nothing in the pipeline writes this yet. The
vocabulary it depends on is real, runnable code (`conduit/materials/`), and is
covered by `tests/test_materials_sizes.py` and
`tests/test_materials_vocabulary.py`.

**Scope rule, from the owner:** the deliverable is **quantities only. No
pricing, no labor hours, no extensions, no totals in dollars.** Prices come
from a pricebook the owner controls and we never see. We produce counts and
lengths; their pricebook stays theirs.

That constraint is what makes §2 (the controlled vocabulary) the most important
part of this document. If our line items cannot be *joined* to their pricebook,
a human re-keys every line, and the product's value evaporates on contact with
the spreadsheet.

---

## 1. The line item

Seven columns, in this order. They are the owner's own workbook columns
(`Commercial_Plumbing_Estimator_Takeoff_Sample.xlsx`, sheet **Detailed
Takeoff**, header row 4) with everything to the right of `Unit` dropped —
`Unit Cost Mat.`, `Mat. Total`, `Labor Hrs/Unit`, `Total Labor Hrs`,
`Labor Cost` are all pricing and all theirs.

| # | Column | Type | Generated from | May be blank? |
|---|---|---|---|---|
| 1 | `Item` | string | section letter + ordinal, e.g. `B04` | no |
| 2 | `Description` | string | **generated** by `render_item_name()` | no |
| 3 | `Size / Spec` | string | `Size.display` | yes (item has no size) |
| 4 | `Material` | string | `Material.token` | yes (item has no material) |
| 5 | `Qty` | decimal(18,4) | `TakeoffLine.quantity` | no |
| 6 | `Unit` | enum | `EA` \| `LF` \| `LS` | no |
| 7 | `Notes / Location` | string | **generated** from evidence, §4 | no |

### 1.1 `Item`

A stable within-export label, not an identity. The owner's workbook groups
lines into lettered sections and numbers within them:

| Section | Meaning in the workbook |
|---|---|
| A | Plumbing fixtures |
| B | Domestic cold & hot water piping |
| C | Sanitary waste, vent & storm |
| D | Valves, specialties & accessories |
| E | Equipment |
| F | Demolition, cap & make-safe, misc |

We reproduce the *structure*, not the letters: sections are derived from
`ItemCategory` + `SystemType`, and the letter is assigned at export time in a
fixed section order. Two consequences worth stating plainly:

* `Item` is **not** the join key to anything. It renumbers when a line is
  added. The join key is §3.
* Sections A–F above are plumbing-shaped. The electrical and HVAC section
  breakdown is an **open question for the owner** (§8) — we should not invent
  a section order for trades whose estimators we have not met.

### 1.2 `Description`

Never free text, never model output, never typed by us. Always:

```python
render_item_name(item_type, material=material, size=size)
#   "{size.display} {material.token} {item_type.token}", empty parts dropped
#   → '1/2" copper 90'   '3/4" copper tee'   '2" PVC 45'
```

Reading `Description` back is `parse_item_name()`, which round-trips. A name
that will not parse is a defect in the writer, not a licence to store a string.

### 1.3 `Size / Spec` and `Material`

`Size / Spec` is `Size.display` and nothing else — see §2.1 for the canonical
forms. `Material` is `Material.token`. Both are *also* embedded in
`Description`, deliberately: the estimator reads the description, the pricebook
join uses the machine key, and the two columns give a spreadsheet user
something to filter and pivot on without parsing strings.

The workbook's `Size / Spec` column sometimes carries a spec rather than a size
(`1.28 GPF`, `75 gal`, `Horizontal`, `Hi-Lo`). That is `SizeKind.LABEL`: an
opaque, normalised string we never try to parse into a number.

### 1.4 `Qty`

`TakeoffLine.quantity` — `Numeric(18,4)`. Counts are integers stored in a
decimal column; lengths are feet to four places. **No rounding at the export
boundary**: an estimator who adds our column and gets a different total than
our total has stopped trusting the tool. Rounding, if wanted, is a display
setting, applied after the sum.

### 1.5 `Unit`

Exactly three values reach the estimator:

| Unit | Meaning | Which items |
|---|---|---|
| `EA` | each, a count | fittings, valves, fixtures, devices, equipment, hangers |
| `LF` | linear feet | pipe, tube, conduit, wire, duct, insulation |
| `LS` | lump sum (quantity is always `1`) | allowances: demolition, temporary services, cleanup |

The unit is **derived from `ItemCategory`**, not chosen per line
(`default_uom()`), so a fitting can never arrive as `LF` because someone
mis-typed a row.

`SF`, `CF`, `LB` and `HR` exist in `UnitOfMeasure` and are **not** exported.
`LB` and `SF` become live if HVAC duct is taken off by fabricated weight or by
sheet-metal area — which is an open question (§8), not a decision.

> **Defect to fix in a later slice.** `UnitOfMeasure` has no `LS` member. It
> has `LOT = "LOT"`. The workbook, and every estimator spreadsheet like it,
> writes `LS`. Either add `LUMP_SUM = "LS"` (additive migration; the enum is
> VARCHAR + CHECK, so it is a data migration, not an `ALTER TYPE`) or render
> `LOT` as `LS` in the writer. Recommendation: **add the member**, because a
> display-time rename is exactly the sort of quiet translation that makes an
> audit trail stop matching what the estimator saw. Listed in §6, not done
> here.

### 1.6 `Notes / Location`

In the owner's workbook this column is informal per-line provenance an
estimator already keeps by hand: `Restrooms – mens/womens`, `Main from mech
room`, `Every 6–8 ft + ends`, `Hot water only`, `Avg cost/fitting`. It is the
column where the estimator writes down *why the number is what it is*.

That is our entire product, written by hand. So we generate it, and it is
**mandatory** — a line with no `Notes / Location` is a bug. Format:

```
<sheet list> · <locator> [· <derivation>]

P-101, P-102 · 14 on P-101, 9 on P-102
P-101 · grid B-3 to D-5 · measured
P-101, P-102 · hot water runs · factored: 1 hanger / 7 ft, 2 ends per run
```

Rules:

1. **Sheet numbers always.** The distinct `Sheet.sheet_number` values that
   contributed, in sheet order, from
   `TakeoffLine.contributing_sheet_numbers`. If a sheet has no readable
   number, its page number is used and it is written `p.7` so it is obvious
   the sheet number was not read.
2. **A locator good enough to find it.** For a multi-sheet line, the per-sheet
   split of the quantity (`14 on P-101, 9 on P-102`) — this is the thing the
   estimator-outreach spec calls per-sheet attribution, and without it a
   comparison against a real takeoff can only compare grand totals. For a
   single-evidence line, a position: grid reference if a grid was read,
   otherwise the bbox centre in sheet coordinates.
3. **Derivation, when the quantity was not directly observed.** `measured`,
   `counted`, `derived: geometry`, or `factored: <rule>`. A factored quantity
   says so in the column the estimator actually reads. See
   `docs/derived-quantities.md`.

`Notes / Location` is a *rendering* of provenance, never the storage of it. The
storage is §3, and the two must agree because the string is generated from the
rows.

---

## 2. The controlled vocabulary

Code: `conduit/materials/`. `vocabulary.py` holds the plumbing vocabulary and
the machinery; `proposed.py` holds the unconfirmed electrical and HVAC words;
`sizes.py` holds the canonical size forms.

A material name is **generated from three registry entries**, never typed:

```
{size.display} {material.token} {item_type.token}
```

so the same fitting always yields the same string, and two ways of writing it
cannot become two lines in a total. The registry rejects a duplicate alias at
import time, because an alias silently shadowed by another entry is precisely
the failure this exists to prevent.

### 2.1 Canonical size form

One display form and one key form per size. **`Size.key` is defined as
`normalize_text(Size.display)`** — literally the stage A normaliser
(`conduit/ingest/textlines.py`), which is what already populates
`TextSpan.normalized_text`. So a size read off a drawing joins to the
vocabulary by string equality, with no second normaliser to drift.

| Kind | Display | Key | Example source on a sheet |
|---|---|---|---|
| `NOMINAL` | `1/2"`, `1-1/4"`, `2"` | `0.5IN`, `1.25IN`, `2IN` | a pipe size tag |
| `REDUCER` | `3/4" x 1/2"` | `0.75IN X 0.5IN` | two size tags either side of a node |
| `RECTANGULAR` | `12x8` | `12X8` | a duct tag |
| `WIRE_GAUGE` | `#12 AWG`, `250 kcmil` | `#12 AWG`, `250 KCMIL` | a homerun tag |
| `LABEL` | `75 gal`, `1.28 GPF` | `75 GAL`, `1.28 GPF` | a schedule cell |
| `NONE` | *(empty)* | *(empty)* | item has no size |

Decisions inside that table, each with its reason:

* **Reducers are written larger-end-first, always.** `1/2" x 3/4"` and
  `3/4" x 1/2"` are the same fitting; ordering them by convention is what makes
  them the same key. Enforced in the constructor, tested.
* **Nominal sizes must land on a trade denominator** (halves through
  sixty-fourths). `1/10"` is not a pipe size; a value that does not reduce to a
  trade fraction raises rather than rendering an odd fraction that looks
  authoritative.
* **Rectangular duct keys as `12X8`, not `12INX8IN`.** The normaliser only
  converts *marked* inches, and nobody writes inch marks on a duct tag. This is
  a real asymmetry between the round and rectangular families; it is recorded
  in the module docstring rather than papered over. Both forms are stable and
  both round-trip.
* **One genuine ambiguity exists and is never guessed.** `2 x 1` is a 2"×1"
  reducer to a plumber and a 2"×1" duct to a sheet-metal estimator. The caller
  knows the `SystemType` of the run it is labelling, so it passes
  `prefer=SizeKind.RECTANGULAR`; the string alone never decides. An
  inch-marked pair is always a reducer regardless of preference.

### 2.2 Materials and item types

Both are frozen dataclasses in a `Registry`:

* `Material` — `code` (the key component, e.g. `COPPER_TYPE_L`), `token` (what
  appears in the name, e.g. `Type L copper`), per-trade confirmation status,
  aliases. Casing in tokens is deliberate: `copper` is lower case, `PVC` and
  `EMT` are acronyms.
* `ItemType` — `code`, `token`, `ItemCategory` (which fixes the unit), per-trade
  status, aliases, optional `size_kind` constraint.

The plumbing set is seeded from the owner's workbook plus standard trade usage:
90 / 45 / 22-1/2 / street 90 / long sweep, tee / reducing tee / san tee / wye /
combo / cross, coupling / no-hub coupling / union / dielectric union / reducer /
bushing / cap / plug / adapter / nipple / P-trap / closet flange; ball, gate,
globe, butterfly, swing and spring check, balancing, angle stop, PRV, RPZ
backflow preventer, thermostatic mixing valve, vacuum breaker, hose bibb, wall
hydrant; the workbook's fixtures, its equipment, hangers, insulation and
allowances.

Each entry records **per trade** where its wording came from:

| `VocabStatus` | Meaning |
|---|---|
| `OWNER_SOURCED` | the word appears in the owner's workbook |
| `TRADE_STANDARD` | ordinary trade usage, not in the workbook, low risk |
| `PROPOSED_PENDING_OWNER` | **we made it up as a starting point** |

### 2.3 PROPOSED — electrical and HVAC

> **The owner has not supplied electrical or HVAC wording.** Everything in
> `conduit/materials/proposed.py` — EMT/IMC/rigid/flex/liquidtight, THHN and
> XHHW, LB/LL/LR and T/C condulets, connectors, boxes, receptacles, switches,
> panelboards; duct, flex duct, radius and mitered elbows, transitions, taps,
> spin-ins, dampers, diffusers, VAV boxes, RTUs — is
> `PROPOSED_PENDING_OWNER_CONFIRMATION`. It is a defensible starting point
> assembled from ordinary trade usage. It is not agreed, and no claim is made
> that these are the words estimators write.

It is one file so replacing it is one edit. `pending_owner_confirmation()`
generates the review list from the data, so it cannot drift out of date with
the vocabulary — including the handful of plumbing-sourced words (`PVC`, `tee`,
`90`, `coupling`, `reducer`, `hanger`…) that are *also* offered to electrical
or HVAC and are equally unconfirmed there.

Current count, generated: **99 (entry, trade) pairs pending owner
confirmation.** A test asserts every electrical and mechanical item type is
still marked proposed; that test is designed to fail the day real wording
lands, which is the prompt to re-run this section.

### 2.4 What the vocabulary deliberately does not do

It does not price, does not carry labor, does not encode a manufacturer or a
model number (that is `ScheduleRow.manufacturer` / `model_number`), and does
not attempt to be complete. It is a starting vocabulary that fails loudly on an
unknown word rather than quietly inventing a line item.

---

## 3. Provenance: how a line item reaches a bounding box

The rule from `AGENTS.md` §5 is that every quantity traces to a sheet id, page
number, bounding box, and the extractor/model version that produced it. Here is
that path concretely, against the ORM as it exists today
(`conduit/db/models.py`).

```
Export row  (Item | Description | Size/Spec | Material | Qty | Unit | Notes)
   │
   │  one row per current TakeoffLine
   ▼
TakeoffLine                      (is_current = true, pipeline_run_id = R)
   │  .quantity, .uom, .item_class, .material_code, .size_label, .discipline
   │  .aggregation_key           ← the stable identity across runs
   │  .derivation                ← counted|measured|derived_geometric|
   │                               factored|manual — read by the estimator
   │  .factor_rule_id, .factor_rule_version, .factor_value, .factor_basis
   │  .contributing_sheet_numbers ← denormalised, feeds Notes / Location
   │
   │  1..N   (evidence_count, and ck_line_requires_evidence forbids zero)
   ▼
TakeoffLineEvidence              (exactly one of six *_id set)
   │  .contribution_qty          ← Σ contributions == TakeoffLine.quantity
   │  .sheet_id, .page_number, .coordinate_space, .bbox_*   (denormalised)
   │  .extractor_version, .confidence
   │
   ├── measurement_id ─────► Measurement
   │                           .polyline_points  (pdf_points, ordered)
   │                           .sheet_id, .page_number, .bbox_*
   │                           .horizontal_length_ft / .vertical_rise_ft
   │                           .rise_source + .rise_justification
   │                           .sheet_scale_id ─► SheetScale (scale + source)
   │                           .extractor_version
   ├── detection_id ───────► Detection
   │                           .bbox_* in raster_px @ .render_dpi
   │                           .model_name, .model_version, .weights_sha256
   │                           .tile_origin_x/y, .tile_size_px  (exact crop)
   ├── schedule_row_id ────► ScheduleRow ─► ScheduleTable  (bbox, extractor)
   ├── text_span_id ───────► TextSpan     (bbox in pdf_points, .role, .source)
   ├── source_takeoff_line_id ─► TakeoffLine   (a factored quantity's basis;
   │                              the cited line has its own evidence, so the
   │                              chain continues rather than stopping here)
   └── review_action_id ───► ReviewAction (actor, before/after, reason)
                                 │
                                 ▼
                        Sheet  .sheet_number, .page_number,
                               media_box_*, .rotation_deg, .render_dpi
                               → the pdf_points ↔ raster_px transform
                                 │
                                 ▼
                        Document .object_key, .sha256   (the immutable upload)
```

Reading a number backwards is therefore: line → evidence rows → each row's
sheet, page and bbox → the sheet's page geometry → the original PDF bytes by
sha256. Model and extractor versions are on the evidence rows *and* frozen on
`ExportJob.model_versions`, so a shipped export stays explicable after the code
moves.

Two links deserve emphasis because they are where this design differs from the
obvious one:

* **`TakeoffLineEvidence` carries the sheet, page and bbox itself**, duplicated
  from the underlying evidence row. That denormalisation is what makes the
  audit worksheet one query instead of five joins, and it means a reviewer
  never has to know which of the five evidence tables a contribution came from
  to find it on the drawing.
* **`Σ TakeoffLineEvidence.contribution_qty == TakeoffLine.auto_quantity`** is
  an invariant, not an aspiration (`03-pipeline-specs.md` §5.3). A quantity
  that cannot be decomposed into contributions is a quantity nobody can check.

---

## 4. Generating `Notes / Location` from that path

```
sheets     = TakeoffLine.contributing_sheet_numbers
per_sheet  = GROUP BY TakeoffLineEvidence.sheet_id
             → SUM(contribution_qty)  per sheet
locator    = per-sheet split when len(sheets) > 1
             else grid ref if available, else bbox centre in sheet coords
derivation = schemas.derivation_label(line.derivation,
                                     factor_rule_id=…, factor_rule_version=…)
             → 'counted' | 'measured' | 'derived: geometry'
             | 'factored: hanger_spacing v1' | 'entered by reviewer'
```

`derivation` now has a home in the schema — `TakeoffLine.derivation` plus the
factor columns (§6.1) — and the label is generated from those columns rather
than written by a caller who might forget. A factored quantity therefore says
so in the column the estimator actually reads, which was the point.

---

## 5. Aggregation identity

`TakeoffLine.aggregation_key` is the deterministic grouping identity
(`03-pipeline-specs.md` §5.2):

```
{discipline}|{item_class}|{material}|{size_label}|{scope}

P|ELBOW_90|COPPER_WROT|0.5IN|doc
P|ELBOW_90|PVC_SCH40|0.5IN|doc
P|HANGER|-|-|doc
```

Built by `conduit.materials.aggregation_key()`, which is
`conduit.materials.item_key()` plus a scope (`doc`, or `sheet:<sheet_number>`)
— one definition, so the vocabulary's key and the stored key cannot diverge.
The components: `item_class` = `ItemType.code`, `material` = `Material.code`,
and the size component is **`Size.key`**, the normalised form, not
`Size.display`. `Size.key` is *defined* as `normalize_text(Size.display)`
(§2.1), so this is a lossless canonicalisation of the `Size / Spec` column, and
the key does not change with how the size happened to be written on a drawing.

**Material is part of the identity, and that is a correctness property.**
Without it the first two keys above are one key: `1/2" copper 90` and
`1/2" PVC 90` sum into one line, one total, and nothing in the export shows
that it happened. An item with no material takes `-`, so the key is always
five fields wide and a missing component can never shift the meaning of a
later one. Tested in `tests/test_takeoff_line_identity.py`, including a
regression assertion stated as the old, material-blind behaviour.

---

## 6. Schema gaps — what landed, and what is still missing

This was a nine-item list. **Five of the nine are now built** — migration
`8b41d7c05a92`, `alembic/versions/20260812_1500_line_identity_and_derivation.py`.
The original numbering is kept below so a reference to "§6 item 8" still means
what it meant.

### 6.1 Landed

| # | What was built | Note |
|---|---|---|
| 1 | `TakeoffLine.material_code String(64)` (nullable); `aggregation_key` is now `{discipline}\|{item_class}\|{material}\|{size_label}\|{scope}`, built by `conduit.materials.aggregation_key()` | §5. The size component is `Size.key`, not `Size.display`. |
| 2 | `TakeoffLine.derivation` — `Derivation` enum, `counted` / `measured` / `derived_geometric` / `factored` / `manual`, NOT NULL, default `counted` | Rendered for the estimator by `schemas.derivation_label()` → `factored: hanger_spacing v1`. Still distinct from `status`, which stays a review lifecycle. |
| 3 | `TakeoffLine.factor_rule_id`, `factor_rule_version`, `factor_value Numeric(18,6)`, `factor_basis` JSON | The rule, its version and the multiplier are **columns**, not JSON keys, because "which lines used `hanger_spacing v1`" is a query a reviewer runs the day a rule turns out to be wrong (`AGENTS.md` §5). `factor_basis` holds the remaining parameters only. `ck_line_factored_carries_factor` requires all three of a line that claims `derivation = factored`. |
| 4 | `TakeoffLineEvidence.source_takeoff_line_id` FK (RESTRICT) + `EvidenceKind.DERIVED_FROM_LINE` | `ck_evidence_exactly_one` was **extended, not relaxed**: exactly one of six rather than exactly one of five. Evidence still cannot be absent, and `ck_line_requires_evidence` is untouched — a factored line satisfies it by having a real evidence row that cites its basis. `ck_evidence_no_self_basis` forbids a line citing itself. |
| 7 | `SystemType` gained `PIPE_DOMESTIC_COLD` / `PIPE_DOMESTIC_HOT` / `PIPE_DOMESTIC_RECIRC` | No DDL: the enum is a `native_enum=False` VARCHAR. `PIPE_DOMESTIC_WATER` is **kept and is not a synonym for cold** — it is the value for a run whose service was not determined, and guessing cold would put insulation on the wrong runs. |

Items 2, 3 and 4 were one defect described three ways: a factored quantity was
unstorable *and* unlabelled. They had to land together or not at all.

### 6.2 Still outstanding

| # | Where | What | Why |
|---|---|---|---|
| 5 | new table | `derived_fitting` — immutable, run-scoped: `sheet_id`, `page_number`, `point_x/point_y` (pdf_points), `vertex_index`, `source_measurement_id`, `rule_id`, `tolerance_used`, `item_type_code`, `confidence`, `extractor_version` | A geometry-derived elbow *has a coordinate*. It deserves a real evidence row of its own rather than being smuggled into a note. Plus a new `EvidenceKind` member, `"derived_fitting"`, and an FK on `TakeoffLineEvidence`. See `docs/derived-quantities.md` §3. |
| 6 | `UnitOfMeasure` | `LUMP_SUM = "LS"` | The enum has `LOT`; every estimator spreadsheet writes `LS`. §1.5. Blocked on §8 question 4, which is the owner's to answer. |
| 8 | `TakeoffLine` | `section_code String(4)` + `line_ordinal Integer` | The `Item` column (`B04`). `cost_code` exists but means a job cost code; overloading it would be a lie in a column name. |
| 9 | `PipelineRun.model_versions` | key `"vocabulary": VOCABULARY_VERSION` | Written by convention, no migration needed — noted here so it is not forgotten. An export whose words have since changed must still say which words it used. |

All four remain additive and nullable-friendly, and land whenever the
aggregator slice starts (`AGENTS.md` §4).

---

## 7. What is not known

* **No real plan set exists** (risk R10). Nothing in this document has been
  tried against a drawing an estimator actually bid. The vocabulary is
  seeded from one sample workbook; whether it covers a real bid's line items
  is unmeasured, and the measurement is: take a completed real takeoff, try to
  express every line in it with this vocabulary, and count what does not fit.
  That number is the vocabulary's coverage. It does not exist yet.
* **No estimator has reviewed the column set.** Dropping the pricing columns is
  the owner's instruction and is safe. Whether `Notes / Location` in the format
  of §1.6 is what an estimator wants to read is not known.
* **The section structure is plumbing-shaped**, taken from one workbook.
* **No accuracy claim is made anywhere in this document**, because none can be.

---

## 8. Open questions for the owner

1. **The electrical fitting words.** Does the estimator write `LB`, `condulet`,
   `Type LB body`? Are conduit couplings and connectors counted at all, or
   carried inside a per-100-ft allowance? Is wire taken off by conductor-foot
   or by circuit-foot with a conductor count? (This is the highest-value
   question here: it decides both wording and *what gets counted*.)
2. **The duct words, and the duct unit.** Is duct taken off by the pound of
   fabricated metal, by the linear foot, or by the square foot of sheet metal?
   All three are in use and the answer changes the `Unit` column, not just the
   wording. Are duct fittings counted individually or folded into a fabrication
   allowance?
3. **Section structure for electrical and HVAC** — the A–F equivalent.
4. **`LS` vs `LOT`** — confirm the workbook's `LS` is the wording to ship.
5. **Fittings by type, confirmed.** The workbook aggregates fittings by size
   (`Copper Fittings – 1/2" (elbows, tees, couplings avg)`). The owner wants
   them by type. Confirm: does the estimator want *both* — by-type lines plus a
   by-size subtotal — or by-type only?
6. **Per-sheet attribution in the sample data.** Already the top ask in
   `validation/estimator-outreach.md`; restated because `Notes / Location`
   cannot be validated without it.
