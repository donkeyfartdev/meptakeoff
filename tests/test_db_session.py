"""Local-profile database behaviour, and the Postgres-only bits it skips."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text

from conduit.db.models import ActorType, Base, Document, Project, ProjectStatus
from conduit.db.session import (
    PG_EXTRA_STATEMENTS,
    apply_dialect_extras,
    conduit_home,
    create_engine_from_env,
    database_url,
    is_postgres,
    next_audit_seq,
    session_factory,
)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUIT_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path.as_posix()}/t.db")
    eng = create_engine_from_env()
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def test_default_url_is_a_local_sqlite_file(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CONDUIT_DATABASE_URL", raising=False)
    monkeypatch.setenv("CONDUIT_HOME", str(tmp_path / "state"))
    url = database_url()
    assert url.startswith("sqlite+pysqlite:///")
    assert url.endswith("conduit.db")
    assert conduit_home().is_dir()


def test_env_var_selects_the_database(monkeypatch) -> None:
    monkeypatch.setenv("CONDUIT_DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    assert database_url() == "postgresql+psycopg://u:p@h/db"


def test_all_16_tables_create_on_sqlite(engine) -> None:
    names = set(inspect(engine).get_table_names())
    assert len(Base.metadata.tables) == 16
    assert set(Base.metadata.tables) <= names


def test_foreign_keys_are_enforced_on_sqlite(engine) -> None:
    """Without PRAGMA foreign_keys=ON this silently succeeds, making local
    tests weaker than production exactly where evidence integrity lives."""
    Session = session_factory(engine)
    with Session() as s:
        s.add(
            Document(
                id=uuid.uuid4(),
                project_id=uuid.uuid4(),  # no such project
                name="x.pdf",
                original_filename="x.pdf",
                object_key="sha256/aa/bb/" + "0" * 64,
                sha256="0" * 64,
                byte_size=1,
                page_count=1,
            )
        )
        with pytest.raises(Exception) as exc:
            s.commit()
        assert "FOREIGN KEY" in str(exc.value).upper()


def test_check_constraints_survive_the_sqlite_translation(engine) -> None:
    """``ck_document_pages_positive`` is a plain CHECK, so SQLite enforces it —
    proof that the copied model's constraints are not decoration locally."""
    Session = session_factory(engine)
    with Session() as s:
        proj = Project(id=uuid.uuid4(), name="P", status=ProjectStatus.ACTIVE)
        s.add(proj)
        s.commit()
        s.add(
            Document(
                id=uuid.uuid4(),
                project_id=proj.id,
                name="x.pdf",
                original_filename="x.pdf",
                object_key="sha256/aa/bb/" + "1" * 64,
                sha256="1" * 64,
                byte_size=1,
                page_count=0,  # violates ck_document_pages_positive
            )
        )
        with pytest.raises(Exception):
            s.commit()


def test_postgres_extras_are_skipped_on_sqlite(engine) -> None:
    with engine.begin() as conn:
        assert is_postgres(conn) is False
        assert apply_dialect_extras(conn) == []
    # ...and are declared, not lost.
    joined = " ".join(PG_EXTRA_STATEMENTS).lower()
    assert "pg_trgm" in joined
    assert "gin (normalized_text gin_trgm_ops)" in joined
    assert "audit_event_seq_seq" in joined


def test_audit_seq_is_emulated_on_sqlite(engine) -> None:
    from conduit.db.models import AuditEvent

    Session = session_factory(engine)
    with Session() as s:
        assert next_audit_seq(s) == 1
        s.add(
            AuditEvent(
                id=uuid.uuid4(),
                seq=next_audit_seq(s),
                event_type="document.uploaded",
                entity_type="document",
                entity_id=uuid.uuid4(),
                actor_type=ActorType.SYSTEM,
                payload={"note": "synthetic"},
                occurred_at=datetime.now(timezone.utc),
            )
        )
        s.commit()
        assert next_audit_seq(s) == 2


def test_partial_unique_indexes_exist_on_sqlite(engine) -> None:
    """The ORM declares ``sqlite_where`` alongside ``postgresql_where``; if that
    were dropped, the index would become fully unique and reject legal rows."""
    with engine.connect() as conn:
        sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE name='uq_pipeline_run_current'")
        ).scalar_one()
    assert "WHERE is_current" in sql
