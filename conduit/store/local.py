"""``LocalFsStore`` — the local-profile object store: a directory on disk.

This is the ONLY module in the package that is allowed to construct filesystem
paths for pipeline data (plus ``conduit.db.session`` for the SQLite file, and
``conduit.bench`` for developer-facing outputs). See
``tests/test_no_direct_paths.py``.

Write protocol (same shape as S3's multipart-then-commit):

1. stream chunks into a temp file under ``<root>/tmp/`` while updating sha256;
2. ``fsync`` + ``os.replace`` into the content-addressed key path.

``os.replace`` is atomic on POSIX, so a reader never observes a half-written
object, and a crash leaves at most a temp file. If the key already exists with
the same size, the write is skipped — identical bytes, identical key.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from conduit.errors import ObjectNotFound, ObjectStoreError
from conduit.store.base import CHUNK_SIZE, ObjectRef, key_for_digest


def default_local_store() -> LocalFsStore:
    """The local-profile object store: ``<CONDUIT_HOME>/objects``.

    Lives here rather than in the caller because this module is the one place
    allowed to build filesystem paths for pipeline data. Entry points ask for
    "the store"; nothing else learns where it is.
    """
    from conduit.db.session import conduit_home

    return LocalFsStore(conduit_home() / "objects")


class LocalFsStore:
    """Content-addressed store rooted at a directory.

    ``root`` is resolved once at construction; nothing else in the codebase
    learns where it is.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).expanduser().resolve()
        self._tmp = self._root / "tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LocalFsStore(root={str(self._root)!r})"

    @property
    def root(self) -> Path:
        """Only tests, ``bench/`` and profile wiring may use this."""
        return self._root

    # --- internals --------------------------------------------------------

    def _path(self, key: str) -> Path:
        # Reject traversal: a key is always the shape key_for_digest() emits.
        parts = key.split("/")
        if len(parts) != 4 or parts[0] != "sha256" or ".." in parts:
            raise ObjectStoreError(f"malformed object key: {key!r}")
        p = (self._root / key).resolve()
        if not str(p).startswith(str(self._root)):
            raise ObjectStoreError(f"object key escapes store root: {key!r}")
        return p

    # --- writes -----------------------------------------------------------

    def put_stream(
        self, chunks: Iterable[bytes], *, content_type: str | None = None
    ) -> ObjectRef:
        import hashlib

        h = hashlib.sha256()
        total = 0
        fd, tmp_name = tempfile.mkstemp(dir=self._tmp, prefix="put-")
        try:
            with os.fdopen(fd, "wb") as tmp_fp:
                for chunk in chunks:
                    if not chunk:
                        continue
                    h.update(chunk)
                    total += len(chunk)
                    tmp_fp.write(chunk)
                tmp_fp.flush()
                os.fsync(tmp_fp.fileno())
            digest = h.hexdigest()
            key = key_for_digest(digest)
            dest = self._path(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                os.unlink(tmp_name)  # identical content already stored
            else:
                os.replace(tmp_name, dest)
                os.chmod(dest, 0o444)  # objects are immutable
            return ObjectRef(key=key, sha256=digest, size_bytes=total, content_type=content_type)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def put_bytes(self, data: bytes, *, content_type: str | None = None) -> ObjectRef:
        return self.put_stream([data], content_type=content_type)

    def put_file(
        self, source: str | os.PathLike[str], *, content_type: str | None = None
    ) -> ObjectRef:
        """Ingest a file already on disk (uploads land here) without slurping it."""

        def _chunks() -> Iterable[bytes]:
            with open(source, "rb") as fp:
                while True:
                    chunk = fp.read(CHUNK_SIZE)
                    if not chunk:
                        return
                    yield chunk

        return self.put_stream(_chunks(), content_type=content_type)

    # --- reads ------------------------------------------------------------

    def open(self, key: str) -> BinaryIO:
        p = self._path(key)
        try:
            return open(p, "rb")
        except FileNotFoundError as exc:
            raise ObjectNotFound(key) from exc

    def get_bytes(self, key: str) -> bytes:
        with self.open(key) as fp:
            return fp.read()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def size(self, key: str) -> int:
        p = self._path(key)
        if not p.exists():
            raise ObjectNotFound(key)
        return p.stat().st_size

    def local_path_for_reader(self, key: str) -> Path:
        """Escape hatch for libraries that insist on a filename.

        PyMuPDF can open a stream, so ingest does not need this; it exists for
        future tools that cannot. On S3 the equivalent is a temp-file download,
        which is why this is a named method rather than callers touching
        ``root`` — the S3 implementation can provide the same contract.
        """
        if not self.exists(key):
            raise ObjectNotFound(key)
        return self._path(key)
