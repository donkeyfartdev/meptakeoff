"""The copied models must stay identical to the design package, and valid.

``design/models/orm.py`` and ``design/models/schemas.py`` are the source of
truth for entity and field names. They were copied here verbatim; this file
proves both that the copies still import and validate, and that they have not
drifted from their source.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DESIGN_MODELS = REPO.parent / "design" / "models"  # sibling checkout, may be absent

COPIES = {
    "orm.py": REPO / "conduit" / "db" / "models.py",
    "schemas.py": REPO / "conduit" / "schemas.py",
}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_copies_import_and_expose_the_16_tables() -> None:
    from conduit.db.models import Base

    assert len(Base.metadata.tables) == 16


def test_schemas_import_through_the_orm_alias() -> None:
    """``schemas.py`` does ``from orm import ...``; the package installs the
    alias so the file stays byte-identical to its source."""
    import conduit.schemas as schemas

    assert schemas.__name__ == "conduit.schemas"
    assert sys.modules["orm"].__name__ == "conduit.db.models"


@pytest.mark.skipif(not DESIGN_MODELS.is_dir(), reason="design package not checked out alongside")
def test_copies_are_byte_identical_to_the_design_package() -> None:
    for source_name, copy_path in COPIES.items():
        src = DESIGN_MODELS / source_name
        assert _sha(src) == _sha(copy_path), (
            f"{copy_path.relative_to(REPO)} has drifted from design/models/{source_name}. "
            "Edit the design package and re-copy; never patch the copy."
        )


@pytest.mark.skipif(
    not (DESIGN_MODELS / "verify_models.py").is_file(), reason="verify_models.py not available"
)
def test_verify_models_passes_against_the_copies(tmp_path: Path) -> None:
    """Run the design package's own checker against THIS repo's copies.

    The files are assembled in a tmp dir under the flat names the checker
    expects, so what is verified is the code that ships here.
    """
    shutil.copy(COPIES["orm.py"], tmp_path / "orm.py")
    shutil.copy(COPIES["schemas.py"], tmp_path / "schemas.py")
    shutil.copy(DESIGN_MODELS / "verify_models.py", tmp_path / "verify_models.py")
    proc = subprocess.run(
        [sys.executable, "verify_models.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL CHECKS PASSED" in proc.stdout, proc.stdout[-2000:]
