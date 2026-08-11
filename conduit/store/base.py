"""``ObjectStore`` protocol and the content-addressing rules every backend obeys.

Content addressing
------------------
Every object is keyed by the sha256 of its bytes:

    sha256/<aa>/<bb>/<full-64-hex-digest>

Consequences we rely on elsewhere:

* **Writes are idempotent and deduplicating.** Re-rendering the same page at
  the same DPI writes the same key, so a detect-only re-run costs zero new
  objects (roadmap W3 "Re-run is cheap").
* **Objects are immutable.** Nothing overwrites a key with different bytes; if
  the content changes, the key changes. Evidence rows can therefore store a key
  and still resolve years later.
* **The digest is the integrity check.** ``ObjectRef.sha256`` is what goes into
  ``Document.sha256`` / raster keys, so provenance is verifiable by re-hashing.

The digest is computed **in flight** while streaming, never by reading the whole
object into memory afterwards (risk R8 — this box has modest RAM and a plan
sheet raster at 200 dpi is tens of MB).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO, Protocol, runtime_checkable

CHUNK_SIZE = 1024 * 256  # 256 KiB: bounded memory per streaming write.


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """A stored object. ``key`` is what goes in the database, never a path."""

    key: str
    sha256: str
    size_bytes: int
    content_type: str | None = None

    @property
    def uri(self) -> str:
        """Store-neutral URI for logs and audit payloads.

        Deliberately not a filesystem path and not an ``s3://`` URL: the same
        object has the same URI on every profile.
        """
        return f"conduit-object://{self.key}"


def key_for_digest(digest: str) -> str:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"not a sha256 hex digest: {digest!r}")
    return f"sha256/{digest[0:2]}/{digest[2:4]}/{digest}"


def sha256_of(chunks: Iterable[bytes]) -> tuple[str, int]:
    """Digest + byte count for an iterable of chunks, without buffering it."""
    h = hashlib.sha256()
    total = 0
    for chunk in chunks:
        h.update(chunk)
        total += len(chunk)
    return h.hexdigest(), total


def iter_file(fp: BinaryIO, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    while True:
        chunk = fp.read(chunk_size)
        if not chunk:
            return
        yield chunk


@runtime_checkable
class ObjectStore(Protocol):
    """The only way pipeline code touches bytes at rest.

    Implementations: ``conduit.store.local.LocalFsStore`` (local profile) and
    ``conduit.store.s3.S3ObjectStore`` (production profile, seam only — see that
    module for the exact boto3 mapping and what is unimplemented).
    """

    def put_bytes(self, data: bytes, *, content_type: str | None = None) -> ObjectRef:
        """Store ``data``; return its ref. Idempotent for identical bytes."""
        ...

    def put_stream(
        self, chunks: Iterable[bytes], *, content_type: str | None = None
    ) -> ObjectRef:
        """Store a stream, hashing in flight. Never buffers the whole object."""
        ...

    def open(self, key: str) -> BinaryIO:
        """Open an object for streaming reads. Caller closes.

        Raises ``conduit.errors.ObjectNotFound`` if the key is absent.
        """
        ...

    def get_bytes(self, key: str) -> bytes:
        """Whole-object read. Only for small objects (manifests, JSON)."""
        ...

    def exists(self, key: str) -> bool: ...

    def size(self, key: str) -> int: ...


@contextmanager
def reading(store: ObjectStore, key: str) -> Iterator[BinaryIO]:
    """``with reading(store, key) as fp:`` — close-safe read for any backend."""
    fp = store.open(key)
    try:
        yield fp
    finally:
        fp.close()
