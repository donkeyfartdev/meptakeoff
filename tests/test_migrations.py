"""The Alembic cycle, on the local profile.

Roadmap W1 DoD: ``alembic upgrade head`` then ``downgrade base`` then
``upgrade head``, clean. Here it runs against a throwaway SQLite file so the
check is repeatable in CI rather than a thing someone did once by hand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

REPO = Path(__file__).resolve().parents[1]


def _alembic(args: list[str], db_url: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "CONDUIT_DATABASE_URL": db_url,
        "PYTHONPATH": str(REPO),
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )


@pytest.fixture(scope="module")
def db_url(tmp_path_factory) -> str:
    return f"sqlite+pysqlite:///{tmp_path_factory.mktemp('mig').as_posix()}/migrate.db"


def test_upgrade_downgrade_upgrade_is_clean(db_url: str) -> None:
    up1 = _alembic(["upgrade", "head"], db_url)
    assert up1.returncode == 0, up1.stdout + up1.stderr

    engine = create_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert len(tables - {"alembic_version"}) == 16, sorted(tables)

        down = _alembic(["downgrade", "base"], db_url)
        assert down.returncode == 0, down.stdout + down.stderr
        assert "Running downgrade" in down.stderr, down.stderr
        remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
        assert remaining == set(), sorted(remaining)

        up2 = _alembic(["upgrade", "head"], db_url)
        assert up2.returncode == 0, up2.stdout + up2.stderr
        assert len(set(inspect(engine).get_table_names()) - {"alembic_version"}) == 16
    finally:
        engine.dispose()


def test_migration_matches_the_models(db_url: str) -> None:
    """After upgrading, autogenerate must find nothing left to do.

    This is what stops the migration and the copied ORM from drifting.
    """
    assert _alembic(["upgrade", "head"], db_url).returncode == 0
    check = _alembic(["check"], db_url)
    assert check.returncode == 0, (
        "alembic check found a difference between conduit/db/models.py and the "
        "migrations:\n" + check.stdout + check.stderr
    )
