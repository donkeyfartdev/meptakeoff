"""Engine and session construction, and the one place dialect differences live.

Profiles
--------
Local (this machine, and any laptop)::

    CONDUIT_DATABASE_URL unset  ->  sqlite+pysqlite:///<CONDUIT_HOME>/conduit.db
    CONDUIT_HOME unset          ->  ./var   (relative to the process CWD)

Production (``02-tech-stack.md``)::

    CONDUIT_DATABASE_URL=postgresql+psycopg://user:pw@host:5432/conduit

Nothing else in the codebase reads either variable, and no absolute path is
baked into any config file, so the tree moves between machines unedited.

=============================================================================
POSTGRES-ONLY FEATURES OF THE ORM, AND WHAT HAPPENS ON SQLITE
=============================================================================
These are declared in ``conduit/db/models.py`` and MUST NOT be deleted from it.
They are skipped or emulated here behind a dialect check, and every one of them
is listed in ``conduit/PROFILES.md`` as untested on the local profile.

1. ``pg_trgm`` GIN index on ``text_span.normalized_text``
   (``models.py`` ~line 733: "Postgres only: CREATE INDEX ... USING gin
   (normalized_text gin_trgm_ops)"). Powers fuzzy tag/text search.
   * Postgres: created by the initial Alembic migration, together with
     ``CREATE EXTENSION IF NOT EXISTS pg_trgm``.
   * SQLite: **skipped entirely.** There is no trigram index; any fuzzy search
     built on it degrades to a full scan with ``LIKE``. Acceptable for a
     24-page synthetic corpus, not a substitute for measuring the query on
     Postgres. ``apply_dialect_extras()`` does nothing here.

2. ``audit_event_seq_seq`` sequence backing ``audit_event.seq``
   (``models.py`` ~line 1315). Gives audit events a gap-free-ish global
   ordering independent of clock skew.
   * Postgres: ``CREATE SEQUENCE audit_event_seq_seq OWNED BY
     audit_event.seq`` + ``ALTER COLUMN seq SET DEFAULT nextval(...)``, added by
     the initial migration.
   * SQLite: no sequences exist. ``next_audit_seq()`` emulates it with
     ``SELECT max(seq) + 1``. That emulation is **not concurrency-safe** and is
     only sound because the local profile runs a single writer process. Do not
     let it become the production path.

3. Partial unique indexes (``... WHERE is_current``) on ``sheet_scale``,
   ``pipeline_run``, ``takeoff_line``. The ORM already declares both
   ``postgresql_where=`` and ``sqlite_where=``, so SQLite gets a real partial
   index too. Nothing to do here — noted because it is the kind of thing that
   silently degrades to a full unique index and starts rejecting legal rows.

4. ``JSONB`` columns are declared as ``JSON().with_variant(JSONB(),
   "postgresql")``, so SQLite stores plain JSON text. Containment operators
   (``@>``) and JSONB indexes are unavailable locally.

5. Server-side ``func.now()`` defaults resolve to ``CURRENT_TIMESTAMP`` on
   SQLite, which is UTC but **not timezone-aware** on read-back. Python-side
   ``default=utcnow`` (also declared in the ORM) is what the application relies
   on; the server default is a backstop.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_SQLITE_FILENAME = "conduit.db"
ENV_URL = "CONDUIT_DATABASE_URL"
ENV_HOME = "CONDUIT_HOME"


def conduit_home() -> Path:
    """Directory for local-profile state (SQLite file, object store root)."""
    return Path(os.environ.get(ENV_HOME, "var")).expanduser()


def database_url() -> str:
    """Resolve the database URL. SQLite local file unless told otherwise."""
    url = os.environ.get(ENV_URL)
    if url:
        return url
    home = conduit_home()
    home.mkdir(parents=True, exist_ok=True)
    # SQLAlchemy wants a POSIX-style path after the third slash.
    return f"sqlite+pysqlite:///{(home / DEFAULT_SQLITE_FILENAME).as_posix()}"


def is_postgres(bind: Engine | Connection) -> bool:
    return bind.dialect.name == "postgresql"


def is_sqlite(bind: Engine | Connection) -> bool:
    return bind.dialect.name == "sqlite"


def create_engine_from_env(url: str | None = None, *, echo: bool = False, **kwargs) -> Engine:
    """Build an Engine for the current profile.

    SQLite gets ``foreign_keys=ON`` per connection — it is OFF by default, and
    without it every ``ForeignKey`` in the ORM is decoration, which would make
    local tests weaker than production in exactly the place that matters
    (evidence rows must not outlive their run).
    """
    url = url or database_url()
    if url.startswith("sqlite"):
        kwargs.setdefault("connect_args", {"check_same_thread": False})
        # Bounded memory: SQLite is a file, no pool of server connections.
    else:
        kwargs.setdefault("pool_pre_ping", True)
    engine = create_engine(url, echo=echo, future=True, **kwargs)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


# --------------------------------------------------------------------------
# Dialect-specific extras
# --------------------------------------------------------------------------

PG_EXTRA_STATEMENTS: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX IF NOT EXISTS ix_text_span_normalized_trgm "
    "ON text_span USING gin (normalized_text gin_trgm_ops)",
    "CREATE SEQUENCE IF NOT EXISTS audit_event_seq_seq OWNED BY audit_event.seq",
    "ALTER TABLE audit_event ALTER COLUMN seq SET DEFAULT nextval('audit_event_seq_seq')",
)

PG_EXTRA_DROP_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE audit_event ALTER COLUMN seq DROP DEFAULT",
    "DROP SEQUENCE IF EXISTS audit_event_seq_seq",
    "DROP INDEX IF EXISTS ix_text_span_normalized_trgm",
)


def apply_dialect_extras(conn: Connection) -> list[str]:
    """Create the Postgres-only objects. No-op (returns []) on SQLite.

    Called by the initial Alembic migration and by ``create_all`` test setup.
    Returns the statements it actually executed so callers can log them.
    """
    if not is_postgres(conn):
        return []
    executed: list[str] = []
    for stmt in PG_EXTRA_STATEMENTS:
        conn.execute(text(stmt))
        executed.append(stmt)
    return executed


def drop_dialect_extras(conn: Connection) -> list[str]:
    if not is_postgres(conn):
        return []
    executed: list[str] = []
    for stmt in PG_EXTRA_DROP_STATEMENTS:
        conn.execute(text(stmt))
        executed.append(stmt)
    return executed


def next_audit_seq(session: Session) -> int:
    """Next ``audit_event.seq``.

    Postgres: the real sequence. SQLite: ``max(seq) + 1``, which is a genuine
    emulation and genuinely not concurrency-safe — see the module docstring.
    """
    bind = session.get_bind()
    if is_postgres(bind):
        return int(session.execute(text("SELECT nextval('audit_event_seq_seq')")).scalar_one())
    current = session.execute(text("SELECT max(seq) FROM audit_event")).scalar()
    return int(current or 0) + 1
