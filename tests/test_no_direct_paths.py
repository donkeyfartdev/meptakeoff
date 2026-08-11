"""Nothing in the pipeline may touch the filesystem directly.

Rasters, tiles and uploads go through ``ObjectStore``; if a stage opens a path,
the same code cannot run against S3, and the local profile stops being a
faithful rehearsal of production. This test greps the package rather than
trusting the convention.

Allowed exceptions, each with a reason:

* ``conduit/store/local.py``  — it *is* the filesystem implementation.
* ``conduit/db/session.py``   — resolves the local SQLite file path.
* ``conduit/bench/*``         — developer tooling that writes corpora and
  results to the working tree by design, not pipeline code.
* ``conduit/db/models.py``, ``conduit/schemas.py`` — verbatim copies from the
  design package; they are data models and contain no I/O, but they are
  excluded from this scan on principle: this repo does not edit them.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "conduit"

ALLOWED = {
    "store/local.py",
    "db/session.py",
    "db/models.py",
    "schemas.py",
    "bench/make_corpus.py",
    "bench/__init__.py",
    "store/base.py",   # protocol declaration: `def open(self, key)`
    "store/s3.py",     # same, for the unimplemented S3 backend
    "pdf/pymupdf_backend.py",  # pymupdf.open(stream=...) — bytes, not a path
}

FORBIDDEN = (
    (r"(?<![.\w])open\s*\(", "builtin open()"),
    (r"\bPath\s*\(", "pathlib.Path()"),
    (r"\bos\.path\b", "os.path"),
    (r"\bshutil\b", "shutil"),
    (r"\btempfile\b", "tempfile"),
)


def _scan(path: Path) -> list[str]:
    hits = []
    src = path.read_text()
    src = re.sub(r'""".*?"""', "", src, flags=re.S)  # drop docstrings
    for lineno, line in enumerate(src.splitlines(), 1):
        code = line.split("#", 1)[0]
        if code.lstrip().startswith(("def ", "async def ")):
            continue
        for pattern, label in FORBIDDEN:
            if re.search(pattern, code):
                hits.append(f"{path.name}:{lineno}: {label}: {line.strip()}")
    return hits


def test_pipeline_modules_do_not_open_filesystem_paths() -> None:
    offenders: list[str] = []
    scanned = 0
    for py in sorted(PACKAGE.rglob("*.py")):
        rel = py.relative_to(PACKAGE).as_posix()
        if rel in ALLOWED:
            continue
        scanned += 1
        offenders.extend(_scan(py))
    assert scanned >= 8, "the scan should cover the whole package"
    assert not offenders, "pipeline code must go through ObjectStore:\n" + "\n".join(offenders)


def test_only_one_module_imports_the_pdf_library() -> None:
    """Companion rule to the ``PdfBackend`` seam (risk R9)."""
    importers = []
    for py in sorted(PACKAGE.rglob("*.py")):
        src = re.sub(r'""".*?"""', "", py.read_text(), flags=re.S)
        for line in src.splitlines():
            code = line.split("#", 1)[0].strip()
            if re.match(r"^(import|from)\s+(pymupdf|fitz)\b", code):
                importers.append(py.relative_to(PACKAGE).as_posix())
    assert sorted(set(importers)) == ["bench/make_corpus.py", "pdf/pymupdf_backend.py"], importers
