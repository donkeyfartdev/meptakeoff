"""Add sheet.paths_object_key.

Revision ID: 4c1e6b2fd08a
Revises: ba9d07f8c7a3
Create Date: 2026-08-12

Why a column and not an audit payload
-------------------------------------
Stage A already writes a ``paths.json.zst`` per page and already records its
object key — but only inside the ``sheet.ingested`` ``AuditEvent`` payload.
That is traceable and not queryable: answering "which sheets have no vector
paths, and where is the dump for sheet E-201" required parsing JSON out of the
audit table. ``AGENTS.md`` §5: provenance a reviewer has to reconstruct out of
a JSON payload is not queryable provenance. So it gets a column.

Additive and nullable: existing rows keep NULL, which is the honest value —
those runs did not record the key anywhere a query can reach. No backfill is
attempted from audit payloads; a re-run at the same DPI repopulates it from
the evidence rather than from an interpretation of the old log.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "4c1e6b2fd08a"
down_revision: str | None = "ba9d07f8c7a3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sheet", sa.Column("paths_object_key", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("sheet", "paths_object_key")
