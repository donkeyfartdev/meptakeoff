"""``paths.json.zst`` — the vector path cache stage E will measure from.

What matters here is that the artifact is *self-describing and lossless*: the
coordinates come back in pdf_points, the geometry survives the round trip, and
the codec is recorded rather than guessed from a filename (there are no
filenames in a content-addressed store).
"""

from __future__ import annotations

import gzip
import json

import pytest

from conduit.ingest.config import StageAConfig
from conduit.ingest.paths_dump import (
    PATHS_SCHEMA,
    available_codec,
    dump_paths,
    encode_paths,
    load_paths,
    paths_to_dict,
)


def test_round_trip_preserves_every_primitive(backend, store) -> None:
    paths = backend.drawings(1)
    assert paths, "page 1 of the corpus has vector art"

    artifact = dump_paths(
        paths,
        page_number=1,
        extractor_version="pymupdf-test",
        store=store,
        cfg=StageAConfig(),
    )
    doc = load_paths(store.get_bytes(artifact.ref.key), artifact.codec)

    assert doc["schema"] == PATHS_SCHEMA
    assert doc["coordinate_space"] == "pdf_points"
    assert doc["extractor_version"] == "pymupdf-test"
    assert doc["path_count"] == len(paths)

    for original, restored in zip(paths, doc["paths"], strict=True):
        assert restored["kind"] == original.kind
        assert restored["seq"] == original.seq
        assert restored["line_width"] == pytest.approx(original.line_width)
        assert restored["bbox"] == pytest.approx(list(original.bbox.as_tuple()))
        assert len(restored["items"]) == len(original.items)
        for item_out, item_in in zip(restored["items"], original.items, strict=True):
            assert item_out["op"] == item_in.op
            flat_out = [c for pt in item_out["points"] for c in pt]
            flat_in = [c for p in item_in.points for c in (p.x, p.y)]
            assert flat_out == pytest.approx(flat_in)


def test_dashed_and_solid_runs_are_distinguishable(backend, store) -> None:
    """Stage E keys on dash patterns; losing them here would be invisible later."""
    doc = paths_to_dict(
        backend.drawings(1), page_number=1, extractor_version="pymupdf-test"
    )
    dashes = {p["dashes"] for p in doc["paths"]}
    assert dashes != {""}, "the corpus draws one dashed run"


def test_codec_falls_back_to_gzip_when_zstandard_is_missing(monkeypatch) -> None:
    """The documented deviation: format changes are recorded, never silent."""
    import builtins

    real_import = builtins.__import__

    def no_zstd(name, *args, **kwargs):
        if name == "zstandard":
            raise ImportError("simulated: zstandard not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_zstd)
    assert available_codec("zstd") == "gzip"

    payload = {"schema": PATHS_SCHEMA, "paths": []}
    blob, codec, raw_len = encode_paths(payload, StageAConfig())
    assert codec == "gzip"
    assert json.loads(gzip.decompress(blob)) == payload
    assert raw_len == len(json.dumps(payload, separators=(",", ":")).encode())


def test_compression_actually_compresses(backend, store) -> None:
    paths = backend.drawings(1)
    payload = paths_to_dict(paths, page_number=1, extractor_version="x")
    blob, codec, raw_len = encode_paths(payload, StageAConfig())
    assert len(blob) < raw_len
    assert load_paths(blob, codec) == payload
