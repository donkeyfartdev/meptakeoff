"""Stage B — sheet classification (``03-pipeline-specs.md`` §1).

Rules, not a model, and deliberately so (§1.5): the label space is 9
disciplines x 23 subtypes, the dominant signal is a decades-old numbering
convention, we have zero training data, and a rule records *which rule fired* —
which is what ``ClassificationMethod`` and the ``sheet.classified`` audit
payload exist for. A classifier here would be a black box in front of a regex.

The modules:

* ``rules.py``   — ``SHEET_NUMBER_RE``, the prefix -> ``Discipline`` table, the
  ordered subtype keyword vote, and ``classification_confidence`` (§1.2, §1.3,
  §1.7). Pure functions over strings.
* ``regions.py`` — title-block region candidates and their scoring (§1.2),
  including rotated pages, done by mapping the display-space regions back
  through the already-tested ``PageGeometry`` transform.
* ``stage_b.py`` — the per-sheet decision, the ``Sheet`` writes, the
  ``PageTaskState`` rows and the ``sheet.classified`` audit events.

The > 15% abstain-rate trigger for building a thumbnail classifier (§1.5) is
measured by ``conduit.bench.run_set`` and recorded in ``bench/RESULTS.md``. It
is a number, not an opinion.
"""

from __future__ import annotations

from conduit.classify.stage_b import (
    ABSTAIN_CONFIDENCE,
    CLASSIFY_VERSION,
    CODE_VERSION,
    ClassifyResult,
    Decision,
    SheetOutcome,
    classify_document,
    decide,
)

__all__ = [
    "ABSTAIN_CONFIDENCE",
    "CLASSIFY_VERSION",
    "CODE_VERSION",
    "ClassifyResult",
    "Decision",
    "SheetOutcome",
    "classify_document",
    "decide",
]
