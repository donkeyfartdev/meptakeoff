"""``ObjectStore`` contract — content addressing is the point.

Written against the protocol, not against ``LocalFsStore``: when the S3/MinIO
implementation is written, parameterise ``store`` and this file is its test
suite unchanged.
"""

from __future__ import annotations

import hashlib

import pytest

from conduit.errors import ObjectNotFound, ObjectStoreError
from conduit.store import LocalFsStore, ObjectStore
from conduit.store.base import key_for_digest

PAYLOAD = b"E-101 LIGHTING PLAN" * 100


def test_local_store_satisfies_the_protocol(store) -> None:
    assert isinstance(store, ObjectStore)


def test_key_is_the_sha256_of_the_content(store) -> None:
    ref = store.put_bytes(PAYLOAD)
    expected = hashlib.sha256(PAYLOAD).hexdigest()
    assert ref.sha256 == expected
    assert ref.key == key_for_digest(expected)
    assert ref.key.endswith(expected)
    assert ref.size_bytes == len(PAYLOAD)


def test_identical_content_gets_the_identical_key(store) -> None:
    a = store.put_bytes(PAYLOAD)
    b = store.put_bytes(bytes(PAYLOAD))  # different object, same bytes
    assert a.key == b.key
    assert a.sha256 == b.sha256
    # ...and only one object exists on disk.
    files = [p for p in store.root.rglob("*") if p.is_file() and p.parent.name != "tmp"]
    assert len(files) == 1


def test_different_content_gets_a_different_key(store) -> None:
    a = store.put_bytes(PAYLOAD)
    b = store.put_bytes(PAYLOAD + b"!")
    assert a.key != b.key


def test_streamed_write_hashes_in_flight_and_matches_whole_write(store) -> None:
    chunks = [PAYLOAD[i : i + 7] for i in range(0, len(PAYLOAD), 7)]
    streamed = store.put_stream(iter(chunks))
    whole = store.put_bytes(PAYLOAD)
    assert streamed.key == whole.key
    assert streamed.size_bytes == whole.size_bytes


def test_roundtrip_bytes_are_unchanged(store) -> None:
    ref = store.put_bytes(PAYLOAD)
    assert store.get_bytes(ref.key) == PAYLOAD
    with store.open(ref.key) as fp:
        assert fp.read(19) == PAYLOAD[:19]
    assert store.size(ref.key) == len(PAYLOAD)
    assert store.exists(ref.key)


def test_stored_objects_are_immutable_on_disk(store) -> None:
    ref = store.put_bytes(PAYLOAD)
    path = store.local_path_for_reader(ref.key)
    assert path.stat().st_mode & 0o222 == 0, "objects must not be writable once stored"


def test_missing_key_raises_object_not_found(store) -> None:
    key = key_for_digest("0" * 64)
    assert store.exists(key) is False
    with pytest.raises(ObjectNotFound):
        store.open(key)
    with pytest.raises(ObjectNotFound):
        store.size(key)


def test_malformed_and_traversing_keys_are_refused(store) -> None:
    for bad in ["../../etc/passwd", "sha256/../../x/y", "not-a-key", "sha256/aa/bb"]:
        with pytest.raises(ObjectStoreError):
            store.exists(bad)


def test_uri_is_store_neutral(store) -> None:
    ref = store.put_bytes(PAYLOAD)
    assert ref.uri.startswith("conduit-object://")
    assert str(store.root) not in ref.uri


def test_put_file_streams_from_disk(tmp_path, store) -> None:
    src = tmp_path / "upload.pdf"
    src.write_bytes(PAYLOAD)
    ref = store.put_file(src, content_type="application/pdf")
    assert ref.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    assert ref.content_type == "application/pdf"


def test_failed_stream_leaves_no_temp_files(store) -> None:
    def exploding():
        yield b"partial"
        raise RuntimeError("upload died")

    with pytest.raises(RuntimeError):
        store.put_stream(exploding())
    leftovers = list((store.root / "tmp").iterdir())
    assert leftovers == []


def test_s3_seam_is_declared_but_refuses_to_pretend() -> None:
    from conduit.store.s3 import S3ObjectStore

    with pytest.raises(NotImplementedError):
        S3ObjectStore("conduit")


def test_store_root_is_the_only_path_knowledge(tmp_path) -> None:
    s = LocalFsStore(tmp_path / "nested" / "objects")
    assert (tmp_path / "nested" / "objects" / "tmp").is_dir()
