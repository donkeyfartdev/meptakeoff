"""Alembic environment.

The URL is resolved by ``conduit.db.session.database_url()`` so migrations obey
the same profile rules as the application: SQLite locally, Postgres in
production, selected by ``CONDUIT_DATABASE_URL``.

``render_as_batch=True`` is set for SQLite because SQLite cannot ALTER most
things in place; batch mode rebuilds the table instead. It is a no-op on
Postgres.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from conduit.db.models import Base
from conduit.db.session import create_engine_from_env, database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine_from_env(_url())
    with engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            # Foreign keys are enforced for application connections (see
            # conduit.db.session), but must be OFF while migrating: SQLite
            # cannot DROP or rebuild a table that another table references, and
            # the schema contains mutually-dependent foreign keys
            # (measurement <-> review_action <-> sheet_scale) which have no
            # valid single drop order. This is the standard Alembic/SQLite
            # batch-migration practice; it applies to migrations only.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
        # SQLite's Alembic impl declares DDL non-transactional, so
        # begin_transaction() is a no-op there and the alembic_version UPDATE
        # is never flushed — the DDL commits implicitly but the recorded
        # revision does not, leaving "upgrade head" silently unrecorded and the
        # next "downgrade base" a no-op. Commit explicitly (SQLAlchemy 2.0
        # commit-as-you-go); on Postgres this commits the already-open
        # transactional DDL, which is equally correct.
        connection.commit()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
