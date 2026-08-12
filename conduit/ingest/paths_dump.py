"""``page_{n}.paths.json.zst`` — the vector path cache stage E measures from.

``01-architecture.md`` §A: paths are extracted **once**, during ingest, and
cached next to the raster so the measurement stage never re-opens the PDF. The
serialisation is deliberately boring JSON — a format anyone can read three
years from now while auditing a quantity — compressed because a dense sheet
has tens of thousands of primitives.

Coordinates are ``pdf_points`` (unrotated, y-UP), exactly as the ``PdfBackend``
seam delivers them and exactly as ``TextSpan``/``Measurement`` store them. The
codec and the extractor version travel *inside* the document, so a cached file
found on its own is still self-describing.

Codec: ``zstd`` when ``zstandard`` is importable, otherwise ``gzip``. The
fallback is recorded in ``conduit/PROFILES.md`` and in every page's audit
payload rather than being a silent format change.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Sequence
from dataclasses import dataclass

from conduit.ingest.config import PathsCodec, StageAConfig
from conduit.pdf.backend import PdfPath
from conduit.store.base import ObjectRef, ObjectStore

__all__ = [
    "PATHS_SCHEMA",
    "available_codec",
    "decode_paths_blob",
    "dump_paths",
    "encode_paths",
    "load_paths",
    "paths_to_dict",
]

PATHS_SCHEMA = "conduit.page_paths/1"


def available_codec(preferred: PathsCodec) -> PathsCodec:
    """The codec we can actually use. ``zstd`` degrades to ``gzip``."""
    if preferred == "zstd":
        try:
            import zstandard  # noqa: F401
        except ImportError:
            return "gzip"
    return preferred


@dataclass(frozen=True, slots=True)
class PathsArtifact:
    ref: ObjectRef
    codec: PathsCodec
    path_count: int
    uncompressed_bytes: int


def paths_to_dict(
    paths: Sequence[PdfPath], *, page_number: int, extractor_version: str
) -> dict:
    return {
        "schema": PATHS_SCHEMA,
        "page_number": page_number,
        "coordinate_space": "pdf_points",
        "extractor_version": extractor_version,
        "path_count": len(paths),
        "paths": [
            {
                "seq": p.seq,
                "kind": p.kind,
                "bbox": [p.bbox.x0, p.bbox.y0, p.bbox.x1, p.bbox.y1],
                "line_width": p.line_width,
                "stroke_color": list(p.stroke_color) if p.stroke_color else None,
                "fill_color": list(p.fill_color) if p.fill_color else None,
                "dashes": p.dashes,
                "closed": p.closed,
                "even_odd": p.even_odd,
                "layer": p.layer,
                "items": [
                    {"op": it.op, "points": [[pt.x, pt.y] for pt in it.points]} for it in p.items
                ],
            }
            for p in paths
        ],
    }


def encode_paths(payload: dict, cfg: StageAConfig) -> tuple[bytes, PathsCodec, int]:
    """JSON -> compressed bytes. Returns ``(blob, codec, uncompressed_len)``."""
    codec = available_codec(cfg.paths_codec)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if codec == "zstd":
        import zstandard

        blob = zstandard.ZstdCompressor(level=cfg.paths_zstd_level).compress(raw)
    else:
        blob = gzip.compress(raw, compresslevel=6, mtime=0)
    return blob, codec, len(raw)


def dump_paths(
    paths: Sequence[PdfPath],
    *,
    page_number: int,
    extractor_version: str,
    store: ObjectStore,
    cfg: StageAConfig,
) -> PathsArtifact:
    payload = paths_to_dict(
        paths, page_number=page_number, extractor_version=extractor_version
    )
    blob, codec, raw_len = encode_paths(payload, cfg)
    ref = store.put_bytes(
        blob,
        content_type="application/zstd" if codec == "zstd" else "application/gzip",
    )
    return PathsArtifact(
        ref=ref, codec=codec, path_count=len(paths), uncompressed_bytes=raw_len
    )


def load_paths(blob: bytes, codec: PathsCodec) -> dict:
    """Inverse of ``encode_paths``; used by tests and, later, by stage E."""
    if codec == "zstd":
        import zstandard

        raw = zstandard.ZstdDecompressor().decompress(blob)
    else:
        raw = gzip.decompress(blob)
    return json.loads(raw.decode("utf-8"))


#: Magic numbers, so a dump found on its own decodes without being told how.
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_GZIP_MAGIC = b"\x1f\x8b"


def decode_paths_blob(blob: bytes) -> dict:
    """Decode a stored dump by sniffing its codec.

    Stage B reads these back through ``Sheet.paths_object_key`` and must not
    have to know which codec the *producing* run used — a fallback from zstd to
    gzip is a per-run fact (``conduit/PROFILES.md``), and a reader that assumes
    one of them silently breaks on a mixed store.
    """
    if blob.startswith(_ZSTD_MAGIC):
        return load_paths(blob, "zstd")
    if blob.startswith(_GZIP_MAGIC):
        return load_paths(blob, "gzip")
    raise ValueError("unrecognised paths dump: neither zstd nor gzip magic")
