"""Material in the takeoff-line identity, and storable factored quantities.

Revision ID: 8b41d7c05a92
Revises: 4c1e6b2fd08a
Create Date: 2026-08-12

Three gaps from ``docs/output-schema.md`` §6, all of which made the output
either wrong or dishonest.

1. ``takeoff_line.material_code``
   ``aggregation_key`` was ``discipline|item_class|size_label|scope``, so a
   1/2" copper 90 and a 1/2" PVC 90 landed on one key, summed into one total,
   and nothing in the export showed that it had happened. The key becomes
   ``discipline|item_class|material|size_label|scope`` and the material
   component gets its own column so a reviewer can filter on it.

   No backfill: the aggregator has never run, so ``takeoff_line`` is empty in
   every environment. If it were not, NULL would be the honest value — a key
   written without material cannot have its material inferred afterwards.

2. ``takeoff_line.derivation`` + the factor columns, and a sixth evidence
   shape ``takeoff_line_evidence.source_takeoff_line_id``.
   A quantity produced by a rule ("one hanger per 7 ft of pipe") had nowhere
   to record that it was factored rather than counted, and no legal evidence
   row: ``ck_evidence_exactly_one`` required exactly one of five FKs, none of
   which can be another line, and ``ck_line_requires_evidence`` then rejected
   the line for having no evidence.

   The constraint is **extended, not relaxed**: still exactly one, now of six.
   Evidence still cannot be absent. A new
   ``ck_line_factored_carries_factor`` additionally requires a factored line
   to name the rule and version that produced it, and
   ``ck_evidence_no_self_basis`` forbids a line citing itself.

3. ``SystemType`` gains ``pipe_domestic_cold`` / ``pipe_domestic_hot`` /
   ``pipe_domestic_recirc``.
   **No DDL.** ``_enum()`` builds ``native_enum=False`` VARCHAR(48) columns, so
   the members live in the Python enum only and adding three is a code change
   with nothing to migrate. Recorded here because the enum widened in the same
   commit and the next reader should not go looking for the missing migration.
   ``pipe_domestic_water`` is kept and is not a synonym for cold: it is the
   value for a run whose service was not determined.

The two check constraints on ``takeoff_line_evidence`` are replaced inside a
batch operation, which SQLite implements as a table rebuild and Postgres as
plain ALTERs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "8b41d7c05a92"
down_revision: str | None = "4c1e6b2fd08a"
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

_DERIVATION = sa.Enum(
    "counted",
    "measured",
    "derived_geometric",
    "factored",
    "manual",
    name="derivation_line",
    native_enum=False,
    length=48,
)

#: The five-way rule as the initial migration wrote it.
_EXACTLY_ONE_OF_FIVE = (
    "(CASE WHEN detection_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN text_span_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN schedule_row_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN measurement_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN review_action_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)

#: The same rule with the factored-line shape added.
_EXACTLY_ONE_OF_SIX = (
    "(CASE WHEN detection_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN text_span_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN schedule_row_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN measurement_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN review_action_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN source_takeoff_line_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)

_FACTORED_CARRIES_FACTOR = (
    "derivation <> 'factored' OR ("
    "factor_rule_id IS NOT NULL AND factor_rule_version IS NOT NULL "
    "AND factor_value IS NOT NULL)"
)

_NO_SELF_BASIS = (
    "source_takeoff_line_id IS NULL OR source_takeoff_line_id <> takeoff_line_id"
)


def upgrade() -> None:
    with op.batch_alter_table("takeoff_line") as batch:
        batch.add_column(sa.Column("material_code", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column(
                "derivation",
                _DERIVATION,
                nullable=False,
                server_default="counted",
            )
        )
        batch.add_column(sa.Column("factor_rule_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("factor_rule_version", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("factor_value", sa.Numeric(18, 6), nullable=True))
        batch.add_column(sa.Column("factor_basis", _JSON, nullable=True))
        batch.create_check_constraint("ck_line_factored_carries_factor", _FACTORED_CARRIES_FACTOR)

    with op.batch_alter_table("takeoff_line_evidence") as batch:
        batch.add_column(sa.Column("source_takeoff_line_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_evidence_source_takeoff_line",
            "takeoff_line",
            ["source_takeoff_line_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.drop_constraint("ck_evidence_exactly_one", type_="check")
        batch.create_check_constraint("ck_evidence_exactly_one", _EXACTLY_ONE_OF_SIX)
        batch.create_check_constraint("ck_evidence_no_self_basis", _NO_SELF_BASIS)

    op.create_index(
        "ix_evidence_source_line", "takeoff_line_evidence", ["source_takeoff_line_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_source_line", table_name="takeoff_line_evidence")

    with op.batch_alter_table("takeoff_line_evidence") as batch:
        batch.drop_constraint("ck_evidence_no_self_basis", type_="check")
        batch.drop_constraint("ck_evidence_exactly_one", type_="check")
        batch.create_check_constraint("ck_evidence_exactly_one", _EXACTLY_ONE_OF_FIVE)
        batch.drop_constraint("fk_evidence_source_takeoff_line", type_="foreignkey")
        batch.drop_column("source_takeoff_line_id")

    with op.batch_alter_table("takeoff_line") as batch:
        batch.drop_constraint("ck_line_factored_carries_factor", type_="check")
        batch.drop_column("factor_basis")
        batch.drop_column("factor_value")
        batch.drop_column("factor_rule_version")
        batch.drop_column("factor_rule_id")
        batch.drop_column("derivation")
        batch.drop_column("material_code")
