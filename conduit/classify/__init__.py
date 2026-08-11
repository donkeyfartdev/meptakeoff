"""Stage B — sheet classification. NOT BUILT YET (Slice A stops before it).

Planned surface, per ``03-pipeline-specs.md`` §1.5–§1.7 and roadmap W1 task 5:

* ``titleblock.py`` — locate the title-block region (right edge / bottom strip)
  from ``PdfTextSpan`` positions, including rotated title blocks, which is why
  ``PdfTextSpan.direction`` crosses the backend seam.
* ``sheet_number.py`` — sheet number + title extraction and normalisation.
* ``discipline.py`` — discipline + subtype rules, ``classification_confidence``,
  and the abstain path (``UNKNOWN`` rather than a guess).
* ``audit.py`` — ``AuditEvent('sheet.classified')`` with the rationale payload.

The > 15% abstain-rate trigger for building the thumbnail classifier is a
week-1 decision to be *measured*, not assumed.
"""
