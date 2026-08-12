# Derived quantities — rules, provenance, and what is honestly a factor

**Status:** specification. None of these rules is implemented in this PR; stage
E (measurement) does not exist yet. This document defines what stage E's output
must support and where each derived number's evidence comes from, so the stages
are built to produce checkable numbers rather than retrofitted to explain them.

Companion to `docs/output-schema.md`. Vocabulary codes referenced here
(`ELBOW_90`, `TEE`, `REDUCER`, `HANGER`, `INSULATION`, `COUPLING`) are the
`ItemType.code` values in `conduit/materials/vocabulary.py`.

---

## 1. The distinction this whole document exists to enforce

Some quantities are observed. Some are computed from geometry we observed. Some
are multiplied out of a rule of thumb. An estimator reading a takeoff cannot
tell them apart unless we say so, and the difference is exactly the difference
between a number they can check and a number they have to trust.

| Grade | Meaning | Provenance available | Example |
|---|---|---|---|
| **counted** | a thing was found on the sheet | bbox of the detection / schedule row / tag | 8 water closets |
| **measured** | a length computed from traced geometry × scale | polyline points + `SheetScale` | 320 LF of 1/2" copper |
| **derived-geometric** | a fitting implied by the *shape* of traced geometry | **a coordinate** — the vertex or node | a 90 at (412.6, 233.1) on P-101 |
| **factored** | a quantity multiplied out of a rule | **none** — only the source quantity and the rule | 85 hangers at one per 7 ft |

The first three carry a coordinate. **A factored quantity does not, and no
amount of presentation can give it one.** Placing hangers at even intervals
along a pipe run would produce coordinates that look like evidence and are not:
nobody drew a hanger there. Synthesised provenance is worse than none, because
it defeats the check it appears to offer.

Therefore:

* A factored quantity is **labelled `factored` in `Notes / Location`**, with the
  factor and its source, on the line the estimator reads.
* A factored quantity's stored "evidence" is the *line it was factored from*
  plus the rule — which the schema cannot currently express (`output-schema.md`
  §6, items 2–4).
* Where a rule can be geometric, **it is geometric**. A per-foot fitting factor
  is not shipped as a default. It exists only as an explicit fallback, §6.

---

## 2. What stage A already gives us to work with

This is not speculative input; it is on disk today.

* **`Sheet.paths_object_key`** → `page_{n}.paths.json.zst`, written by stage A,
  read back with `decode_paths_blob()` (codec sniffed from magic bytes). Schema
  `conduit.page_paths/1`, coordinates in **`pdf_points`**, unrotated, y-up.
  Each path carries `seq`, `kind`, `bbox`, `line_width`, `stroke_color`,
  `fill_color`, `dashes`, `closed`, `even_odd`, `layer`, and `items` — a list
  of `{"op": …, "points": [[x, y], …]}`.
* **`TextSpan`** rows with `role = SIZE_LABEL` and `normalized_text` in the
  `0.75IN` form — which is exactly `Size.key` (`output-schema.md` §2.1), so a
  size tag joins to the vocabulary by equality.
* **`SheetScale`** with `feet_per_paper_point` and a `ScaleSource`, so a paper
  length becomes feet with the scale's own provenance attached.
* **`Measurement`** (stage E's output, not yet written) with
  `polyline_points` in `pdf_points`, `size_label`, `system_type`, and the
  three-part length model.

`line_width`, `dashes` and `stroke_color` matter more than they look: on a
plumbing sheet, cold water, hot water, and vent are routinely distinguished by
dash pattern and weight rather than by any text. They are the raw material for
`SystemType` assignment, which in turn decides whether insulation applies (§7).

---

## 3. Building the graph the rules run on

Every rule below operates on a **run graph**, not on raw paths. Constructing it
is where most of the risk lives, so it is specified first.

```
paths.json.zst
  → filter to stroked polyline/line items on the run's layer/style class
  → flatten curves (§5.1)                       ← records that it flattened
  → deduplicate coincident segments (§5.3)
  → snap endpoints within ε_snap
  → node graph: nodes = snapped endpoints, edges = segments
  → chains: maximal paths between nodes of degree ≠ 2
```

Parameters, **all of them guesses until measured on a real plan set**:

| Parameter | Proposed start | What it does | How it gets settled |
|---|---|---|---|
| `ε_snap` | `max(1.0 pt, 0.75 × line_width)` | joins endpoints that a drafter meant to meet | sweep on labelled nodes; report node-count stability |
| `θ_elbow` | 90° ± 7° | vertex counts as a 90 | sweep; report abstain rate and disagreement |
| `θ_45` | 45° ± 7° | vertex counts as a 45 | same sweep |
| `θ_straight` | < 3° | vertex is drafting noise, not a fitting | same sweep |
| `θ_branch` | 90° ± 15° | a branch leaves a main at right angles | same sweep |
| `arc_window` | ≤ 6 × line_width | span over which small turns are read as one sweep | §5.1 |

> **These numbers are guesses.** They are starting values chosen to be
> plausible, not results. Nothing has been measured, because no real plan set
> exists (risk R10). The measurement that settles every one of them is the
> same: on a real plan set, hand-label N nodes on M sheets with the fitting an
> estimator would count there, then sweep each threshold and report two curves
> — the fraction of labelled nodes the rule *abstains* on, and the fraction it
> classifies differently from the label. We publish those curves and pick the
> knee. Until then, no threshold in this table is defensible and none should be
> quoted as if it were.

---

## 4. The geometric fitting rules

Each rule states its predicate, what it emits, and its provenance. "Emits" is a
prospective `derived_fitting` evidence row (`output-schema.md` §6, item 5) plus
a contribution to a `TakeoffLine` whose `item_class` is the named vocabulary
code.

### R1 — Elbow, 90°  → `ELBOW_90`

**Predicate.** An interior vertex of a chain whose turn angle
`|Δθ| ∈ θ_elbow`, and which is not part of a sweep (R9) or a junction (R3).

**Emits.** One `ELBOW_90`, size = the chain's governing size, material = the
run's material.

**Provenance.** The vertex coordinate in `pdf_points`, the `Measurement` it
belongs to, the vertex index, and the tolerance used. **Grade:
derived-geometric.** An estimator can be shown the exact corner.

### R2 — Elbow, 45°  → `ELBOW_45`

As R1 with `|Δθ| ∈ θ_45`. Same provenance grade.

### R3 — Turn that is neither → **abstain**

**Predicate.** `θ_straight < |Δθ|` and `Δθ` outside both `θ_elbow` and `θ_45`.

**Emits.** Nothing. A review flag on the sheet with the coordinate.

This rule is the reason the other two are trustworthy. A vertex at 63° is
either a drafting artefact, a field-bend, or a fitting we have no word for;
bucketing it into the nearest of 45 and 90 would inflate a total by an amount
nobody can see. **Abstaining is a measurable outcome (the abstain rate);
guessing is not.**

### R4 — Tee  → `TEE`

**Predicate.** A node of degree 3 where two incident edges are near-collinear
(the main, `|Δθ| < θ_straight` across the node) and the third leaves at
`θ_branch`.

**Emits.** One `TEE`, size = main size, or `TEE_REDUCING` when the branch
carries a different size tag (R7's mechanism).

**Provenance.** The node coordinate, all three incident `Measurement` ids.
**Grade: derived-geometric.**

**Wye variant.** Where the branch leaves at 45° ± `θ_elbow`/2 *and* the system
is DWV (`SystemType.PIPE_SANITARY` / `PIPE_STORM`), the fitting is a `WYE`, not
a tee — the trade does not put sanitary tees on horizontal drainage. This rule
is trade knowledge, is stated here so it can be argued with, and is **not**
validated against anything.

### R5 — Cross, or a crossover that is not a fitting → mostly **abstain**

**Predicate.** A node of degree 4.

**Emits.** Nothing by default. If the four edges form two near-collinear pairs,
this is almost certainly two pipes **crossing without connecting** — extremely
common on plan views, and counting a `CROSS` there would be a fabricated
fitting on a real bid. Emit a review flag. A genuine cross is rare enough that
requiring a human is the right trade.

**Provenance.** Coordinate on the flag. **Grade: abstain.**

### R6 — Reducer  → `REDUCER`

**Predicate.** Along a connected chain, the governing size tag changes: two
`TextSpan`s with `role = SIZE_LABEL` associate to consecutive segments of the
same chain and their `normalized_text` differ.

**Emits.** One `REDUCER` with `Size.reducer(larger, smaller)` — written
larger-end-first by construction, so the same transition is always the same key.

**Provenance.** The two size-tag bboxes, the chain, and the node between the
two differently-sized segments — or, when the size changes mid-segment with no
node, the midpoint between the two tag attachment points, **flagged as
approximate**. **Grade: derived-geometric, with the weakest coordinate of the
set.** The tag-to-segment association is itself an inference (nearest segment
within a radius, leader-line following) and its own error source.

### R7 — Termination → **abstain**

**Predicate.** A node of degree 1 that is not within `ε_connect` of a fixture
detection, an equipment detection, or a sheet boundary / match-line.

**Emits.** Nothing — a review flag. A dead end is at least as likely to be a
run that continues on another sheet, or a run whose connection we failed to
snap, as it is a `CAP`. Counting caps at dead ends would convert our own
tracing failures into billable material.

### R8 — Sheet-boundary continuation → **abstain, and flag the pair**

**Predicate.** A degree-1 node within `ε_edge` of a match line or sheet edge.

**Emits.** Nothing, but records the stub so cross-sheet run stitching (a later
slice) has somewhere to start, and so the same run is not double-counted from
both sheets.

### R9 — Sweep / bend, not N elbows

**Predicate.** A consecutive vertex sequence where each turn is
`< θ_elbow_min` (small) and the cumulative turn approaches 90° or 45° within an
arc span `≤ arc_window`.

**Emits.** In plumbing, one elbow of the cumulative angle — a long-sweep 90 is
still one fitting. In electrical, **nothing**: a sweep in EMT is a field bend
and costs labour, not a fitting.

This rule exists because of §5.1: a PDF arc arrives as a flattened polyline of
a dozen tiny segments, and R1 applied naively to it invents a dozen elbows.
Without R9 the fitting count on any sheet drawn with arcs is nonsense.

---

## 5. Failure modes, named

### 5.1 Curves and sweeps versus true elbows

PDF drawing operators include Béziers. Stage A stores the curve items in
`paths.json.zst` with their control points and the op that produced them, so
flattening is *our* choice at measurement time and is recorded. But CAD
exporters also flatten arcs themselves before writing the PDF, and then no
marker survives at all: a swept 90 and a mitred 90 are both just vertices.

Consequence: R9 is a heuristic over a lossy input, and there is a class of
drawing where a sweep and a series of short offsets are geometrically
indistinguishable. The honest handling is to emit one fitting and flag the
sheet's sweep count, so a reviewer can see how much of the total depends on
this rule.

### 5.2 Drawn-to-scale versus schematic

Risers, one-lines and isometrics are **not to scale**. Their topology is
usually correct; their lengths are meaningless.

Rule: where the sheet's subtype is `P_RISER` / `E_ONE_LINE`, or where no
`SheetScale` with usable confidence exists, **lengths abstain entirely** — no
`Measurement` with a length is written. Fitting *counts* may still be derived
from topology, because a tee on a riser diagram is a real tee, but every such
fitting is flagged `schematic` so a reviewer knows the coordinate locates it on
a diagram rather than in the building.

The trap this avoids is the worst failure this product can have: a plausible
LF number computed off a schematic at a default scale, indistinguishable in the
export from a measured one.

### 5.3 Coincident and duplicated lines

CAD output routinely contains the same line stroked more than once — layered
exports, hatch boundaries over object lines, a pipe drawn over its own
centreline. Undeduplicated, this doubles a length and turns clean vertices into
degree-4 nodes.

Dedup rule: two segments collapse when their endpoints pair within `ε_snap`
*and* their direction agrees within `θ_straight`. Deduplication happens before
node building, and the count of collapsed segments per page is recorded — a
page with an unusual dedup ratio is a page whose numbers deserve a look.

### 5.4 Tag-to-run association

Every size-dependent rule (R6, and the size on R1/R2/R4) rests on associating a
size tag with a run. That association — nearest segment within a radius, or
following a leader line — is a separate inference with its own error rate, and
it is **unmeasured**. Where no tag associates, the run's size is `UNKNOWN` and
its fittings aggregate under a size-unknown key rather than being assigned a
default. A default size here would be a fabricated number in a priced line.

### 5.5 The graph is only as good as the trace

All of §4 presumes stage E correctly traced the run in the first place. If a
run is traced through a wall or stops short, the fittings derived from it are
wrong in a way that no rule here can detect. Fitting counts are therefore
**never more reliable than the lengths they came from**, and both must be
reviewable together.

---

## 6. The rules that are unavoidably factors

### 6.1 Hangers and supports → `HANGER`, **factored**

The owner's workbook: `Pipe Hangers / Supports (copper) … 85 EA …
Every 6–8 ft + ends`.

```
hangers(run) = ceil(run_length_ft / spacing_ft) + terminations
```

* `spacing_ft` is a **project setting**, defaulting per material and size from
  the applicable code's support-spacing table, which the estimator selects.
  It is not a number we invent, and this document does not quote one: the
  spacing table belongs to the code edition in force on that job, and our
  default must be configurable and displayed.
* Provenance: the `TakeoffLine` for the pipe it was factored from, the spacing
  used, and where the spacing came from. **No coordinate. Grade: factored.**
* `Notes / Location` reads e.g.
  `P-101, P-102 · 1/2" copper runs · factored: 1 per 7 ft + 2 ends per run`.

Why not place them geometrically: hangers are almost never drawn on a plan
view. Deriving positions would be inventing evidence (§1).

### 6.2 Couplings → `COUPLING`, **factored**

Couplings come from stock length, not from the drawing: a 20 ft run of hard
copper needs a joint wherever two sticks meet, and nothing on the sheet says
where.

```
couplings(chain) = max(0, ceil(chain_length_ft / stock_length_ft) - 1)
```

`stock_length_ft` is a per-material project setting. **Factored**, labelled,
provenance = the chain it came from. Couplings that *are* geometric — a no-hub
coupling at a joint drawn on the sheet — come from detection or from R4/R6 and
are a different, counted line.

### 6.3 Fittings per foot → **fallback only, never a default**

The workbook's own approach is `Copper Fittings – 1/2" (elbows, tees,
couplings avg) … 95 EA … Avg cost/fitting`: fittings aggregated by size, with
type averaged away. The owner wants them **by type**, and §4 is how.

A per-foot fitting factor therefore ships only as an explicit fallback, used
when geometry is unavailable — hand-drawn or scanned sheets, or a schematic
where topology could not be built. When used it is:

* opt-in per sheet or per system, never silently applied;
* labelled `factored: <n> fittings per <m> ft` in `Notes / Location`;
* aggregated to a single `fittings (assorted)` line **rather than being split
  into 90s and tees**, because splitting a factor by type invents a type
  distribution nobody measured.

### 6.4 Insulation → `INSULATION`, **measured if the schema allows, factored if not**

The workbook: `Insulation – 1/2" wall fiberglass w/ ASJ (hot water) … 380 LF …
Hot water only`.

The rule is simple — insulation LF = the LF of the runs it applies to — and it
is *measurable* with real provenance, inheriting the `Measurement` rows of the
insulated runs. Except:

> **`SystemType` cannot currently express "hot".** It has
> `PIPE_DOMESTIC_WATER` and no hot/cold/recirculation distinction. Until that
> is fixed (`output-schema.md` §6, item 7), insulation cannot be derived by
> rule at all — only factored off a proportion of domestic water LF, which is
> a guess with no coordinate. **The schema change is what turns this quantity
> from factored into measured**, which is a good illustration of why the list
> in §6 of the output schema is not cosmetic.

Insulation of fittings and valves (an adder per fitting) is a **factor** in all
cases.

### 6.5 Vertical rise — the precedent

Vertical rise is already modelled correctly and is worth pointing at: rise is
zero unless `RiseSource` says otherwise, `ck_meas_rise_justified` makes the
database refuse a non-zero rise with no source, and `rise_justification` is
human-readable. Derived fittings and factored quantities should follow exactly
that pattern: **a derived number that cannot name its justification does not
get written.**

---

## 7. Summary — grade per rule

| Rule | Emits | Grade | Coordinate? |
|---|---|---|---|
| R1 elbow 90 | `ELBOW_90` | derived-geometric | vertex |
| R2 elbow 45 | `ELBOW_45` | derived-geometric | vertex |
| R3 other angle | review flag | abstain | vertex |
| R4 tee / wye | `TEE`, `WYE` | derived-geometric | node |
| R5 degree-4 | review flag | abstain | node |
| R6 reducer | `REDUCER` | derived-geometric (weak) | node or tag midpoint, approximate |
| R7 dead end | review flag | abstain | node |
| R8 match-line stub | stitching hint | abstain | node |
| R9 sweep | one elbow (P) / nothing (E) | derived-geometric | arc start |
| 6.1 hangers | `HANGER` | **factored** | none |
| 6.2 couplings | `COUPLING` | **factored** | none |
| 6.3 fittings/ft fallback | `fittings (assorted)` | **factored** | none |
| 6.4 insulation | `INSULATION` | measured *(blocked on schema)* / factored | inherits run |
| 6.4b fitting insulation | `INSULATION` | **factored** | none |

Four of thirteen are factors, and all four say so on the line the estimator
reads.

---

## 8. What is unknown, stated plainly

* **Every tolerance in §3 is a guess.** None has been measured. The protocol
  that settles them is in §3 and needs a real plan set (risk R10).
* **No precision or recall figure exists for any rule here**, and none will be
  quoted from synthetic geometry — the synthetic corpus contains exactly the
  shapes the generator was told to draw, so measuring these rules against it
  would measure the generator.
* **The wye-versus-sanitary-tee rule (R4) is trade knowledge, not evidence.**
  An estimator may disagree; that disagreement is a cheap conversation and an
  expensive silent default.
* **Cross-sheet run stitching is out of scope here** (R8 only records stubs).
  Until it exists, a run crossing a match line is two runs, and its fittings
  are counted at both ends of the break — a known, named over-count rather than
  a surprise.
* **Whether estimators will accept derived fittings at all** is unknown. The
  fallback position — ship the geometry, show the coordinates, let the
  estimator confirm per sheet — is cheaper to build than to argue about, and is
  the reason every rule above emits reviewable evidence rather than a number.
