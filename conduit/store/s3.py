"""S3 / MinIO object store — production-profile seam. NOT IMPLEMENTED YET.

This module exists so the production path stays a *class to write*, not a
refactor. It is deliberately not implemented here: boto3 is not installed on
this machine and no MinIO can run on it (see ``conduit/PROFILES.md``), so an
implementation shipped now would be untested code claiming to work.

What the implementation is, when it is written
----------------------------------------------
Same ``ObjectStore`` protocol, same content-addressed keys
(``sha256/aa/bb/<digest>``), same bucket for both MinIO and AWS S3:

``put_stream``
    Multipart upload with 8 MiB parts, hashing in flight into the same
    ``hashlib.sha256`` used by ``LocalFsStore``. The digest is only known at the
    end, so the upload targets a temporary key (``staging/<uuid>``) and is
    finalised with ``copy_object`` to the content-addressed key followed by
    ``delete_object`` on the staging key. Do NOT use S3's ``ETag`` as the
    digest: for multipart uploads it is a hash of part hashes, not of the
    object.
``put_bytes``
    ``put_object`` directly, digest computed before the call.
``open``
    ``get_object(...)["Body"]`` — a streaming ``botocore`` response object;
    wrap it so callers only ever see a ``BinaryIO``.
``exists`` / ``size``
    ``head_object``; translate ``ClientError`` 404/NoSuchKey into
    ``conduit.errors.ObjectNotFound`` and everything else into
    ``ObjectStoreError``.
``local_path_for_reader``
    Download to a ``tempfile.NamedTemporaryFile`` and return its path, with the
    caller responsible for deleting it. Same contract as the local store's.

Configuration (env, mirroring the local profile's ``CONDUIT_*`` convention)::

    CONDUIT_S3_ENDPOINT_URL   http://minio:9000   (unset for real AWS S3)
    CONDUIT_S3_BUCKET         conduit
    CONDUIT_S3_REGION         us-east-1
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   standard boto3 resolution

Verification when implemented: the shared contract suite in
``tests/test_object_store_contract.py`` must pass against it unchanged — that
suite is written against the protocol, not against ``LocalFsStore``.

Licensing note: MinIO's licence position is one of the four items in the
week-2 licensing memo (``05-roadmap.md``, AGPL checkpoint). The S3 *API* is what
this seam depends on, so SeaweedFS or Garage substitute without code changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import BinaryIO

from conduit.store.base import ObjectRef


class S3ObjectStore:
    """Placeholder. Every method raises ``NotImplementedError`` on purpose."""

    def __init__(self, bucket: str, *, endpoint_url: str | None = None) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        raise NotImplementedError(
            "S3ObjectStore is a documented seam, not an implementation. "
            "See the module docstring for the exact boto3 mapping, and "
            "conduit/PROFILES.md for why it is not built on this profile."
        )

    def put_bytes(self, data: bytes, *, content_type: str | None = None) -> ObjectRef:
        raise NotImplementedError

    def put_stream(
        self, chunks: Iterable[bytes], *, content_type: str | None = None
    ) -> ObjectRef:
        raise NotImplementedError

    def open(self, key: str) -> BinaryIO:
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def size(self, key: str) -> int:
        raise NotImplementedError
