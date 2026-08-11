"""Conduit Takeoff.

Auditable MEP quantity takeoff from multi-sheet construction PDF plan sets.

Layout
------
``conduit.db.models``   ORM models, copied VERBATIM from the design package
                        (``design/models/orm.py``). Do not edit here — edit the
                        design package and re-copy, so the two never diverge.
``conduit.schemas``     Pydantic API/IO schemas, copied VERBATIM from
                        ``design/models/schemas.py``.
``conduit.db.session``  Engine/session factory (SQLite local, Postgres prod).
``conduit.store``       Object store seam (local FS now, S3/MinIO later).
``conduit.pdf``         ``PdfBackend`` protocol + PyMuPDF implementation.
``conduit.geometry``    pdf_points <-> raster_px transforms.
``conduit.ingest``      Stage A (not built yet — Slice A stops before it).
``conduit.classify``    Stage B (not built yet).
``conduit.bench``       Corpus generation and measurement harnesses.

Why the ``sys.modules["orm"]`` alias below
------------------------------------------
``design/models/schemas.py`` imports the ORM enums with a FLAT import
(``from orm import ...``) because in the design package both files sit in the
same directory. The roadmap (W1 task 2) requires both files to be copied in
*unchanged*, so instead of editing that import line we register the copied
module under the name ``orm`` before ``conduit.schemas`` is imported. Importing
``conduit.schemas`` always executes this package ``__init__`` first, so the
alias is guaranteed to be in place.

If the design package is ever restructured to use package-relative imports,
delete this block and the copies stay byte-identical to their sources.
"""

from __future__ import annotations

import sys as _sys

__version__ = "0.1.0"


def _install_orm_alias() -> None:
    from conduit.db import models as _models

    _sys.modules.setdefault("orm", _models)


_install_orm_alias()
