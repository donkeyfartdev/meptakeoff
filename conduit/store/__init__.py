"""Object store seam.

Rule for the whole codebase: **no pipeline module opens a filesystem path.**
Rasters, tiles, ``paths.json.zst``, uploaded PDFs and export artifacts are all
read and written through an ``ObjectStore``. That is what lets the local
profile (a directory on disk) and the production profile (S3/MinIO) be the same
code, and it is what makes ``bench/`` reproducible on a laptop.

``tests/test_no_direct_paths.py`` enforces the rule mechanically.
"""

from conduit.store.base import ObjectRef, ObjectStore, sha256_of
from conduit.store.local import LocalFsStore

__all__ = ["LocalFsStore", "ObjectRef", "ObjectStore", "sha256_of"]
