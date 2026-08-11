"""The WebP deep-zoom pyramid, built from ``clip=`` sub-renders.

Why a pyramid at all
--------------------
An ARCH E sheet at 200 dpi is 9600x7200 px. No browser is handed that as one
image, and no detector sees it as one tensor. ``02-tech-stack.md`` §10 fixes
the format: a standard ``{z}/{x}/{y}`` grid of 256 px WebP tiles, pre-rendered
from the same pixmap the detector will see, so an overlay box lands on the
pixel the model looked at.

How the levels are produced, and the one honest compromise
----------------------------------------------------------
``z = max_zoom`` is the base level at ``render_dpi``; each level down halves
the dimensions. Level ``z`` therefore wants ``render_dpi / 2**(max_zoom - z)``:

* while that quotient is a whole number **and** at least ``MIN_BACKEND_DPI``
  (200, 100, 50 for the default 200 dpi base), tiles are rendered directly by
  ``render_page(clip=...)`` — no intermediate full-page raster ever exists;
* below that (25 dpi and coarser) the backend cannot be asked for the DPI at
  all — ``PageGeometry`` rejects < 36 dpi, mirroring ``ck_sheet_dpi``. Those
  levels, and the thumbnail, are downsampled from **one** 36 dpi page render
  (an ARCH E sheet is 1728x1296 px there, ~6.7 MB), taken inside a single
  ``page_pixels`` scope. So the invariant still holds: one raster live at a
  time, and the largest one is 6.7 MB rather than 207 MB.

Levels marked ``source="downsampled"`` are resampled pixels, not re-rendered
vector art. That is a real difference at high zoom-out and is recorded per
level in the manifest rather than left for someone to discover.

Where the tiles go
------------------
The object store is content-addressed (``sha256/aa/bb/<digest>``), so there is
no ``tiles/{z}/{x}/{y}.webp`` path to point at. Instead each page gets a **tile
manifest** — a small JSON object listing every level's geometry and the object
key of every tile — and ``Sheet.tile_base_key`` holds that manifest's key.
Deduplication falls out for free: the blank tiles that dominate a plan sheet
collapse to one stored object.
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass
from typing import Literal

from conduit.geometry import BBox, PageGeometry
from conduit.ingest.config import MIN_BACKEND_DPI, StageAConfig
from conduit.ingest.render import page_pixels
from conduit.pdf.backend import PdfBackend, RenderedPage
from conduit.store.base import ObjectStore

__all__ = [
    "TileLevel",
    "TilePyramidPlan",
    "TilePyramidResult",
    "build_pyramid",
    "plan_pyramid",
]

TILE_MANIFEST_SCHEMA = "conduit.tile_manifest/1"
LevelSource = Literal["direct", "downsampled"]


@dataclass(frozen=True, slots=True)
class TileLevel:
    z: int
    denominator: int  # 2 ** (max_zoom - z)
    width_px: int
    height_px: int
    cols: int
    rows: int
    source: LevelSource
    render_dpi: int  # the DPI actually requested from the backend

    @property
    def tile_count(self) -> int:
        return self.cols * self.rows


@dataclass(frozen=True, slots=True)
class TilePyramidPlan:
    tile_size: int
    base_width_px: int
    base_height_px: int
    base_dpi: int
    max_zoom: int
    levels: tuple[TileLevel, ...]

    @property
    def tile_count(self) -> int:
        return sum(lvl.tile_count for lvl in self.levels)


@dataclass(frozen=True, slots=True)
class TilePyramidResult:
    manifest_key: str
    max_zoom: int
    tile_count: int
    stored_object_count: int  # distinct keys — blank tiles dedupe
    bytes_written: int
    thumbnail_key: str | None


def plan_pyramid(
    width_px: int,
    height_px: int,
    *,
    tile_size: int = 256,
    base_dpi: int = 200,
    min_backend_dpi: int = MIN_BACKEND_DPI,
) -> TilePyramidPlan:
    """Pure arithmetic: the level list for a page of ``width_px x height_px``.

    ``z = max_zoom`` is the base; ``z = 0`` fits inside a single tile. Level
    dimensions use ``ceil``, matching the deep-zoom convention and the way
    ``PageGeometry`` sizes a render.
    """
    if width_px < 1 or height_px < 1:
        raise ValueError(f"degenerate page raster: {width_px}x{height_px}")
    max_zoom = 0
    while max(_scaled(width_px, 2**max_zoom), _scaled(height_px, 2**max_zoom)) > tile_size:
        max_zoom += 1

    levels: list[TileLevel] = []
    for z in range(max_zoom + 1):
        denom = 2 ** (max_zoom - z)
        w = _scaled(width_px, denom)
        h = _scaled(height_px, denom)
        quotient = base_dpi / denom
        direct = quotient.is_integer() and quotient >= min_backend_dpi
        levels.append(
            TileLevel(
                z=z,
                denominator=denom,
                width_px=w,
                height_px=h,
                cols=math.ceil(w / tile_size),
                rows=math.ceil(h / tile_size),
                source="direct" if direct else "downsampled",
                render_dpi=int(quotient) if direct else min_backend_dpi,
            )
        )
    return TilePyramidPlan(
        tile_size=tile_size,
        base_width_px=width_px,
        base_height_px=height_px,
        base_dpi=base_dpi,
        max_zoom=max_zoom,
        levels=tuple(levels),
    )


def _scaled(value: int, denom: int) -> int:
    return max(1, math.ceil(value / denom))


# --- pixels ---------------------------------------------------------------


def _image_from_rendered(rendered: RenderedPage):
    """``RenderedPage`` -> PIL image, without a PDF library in sight."""
    from PIL import Image

    mode = "L" if rendered.channels == 1 else "RGB"
    if rendered.encoding != "raw":
        return Image.open(io.BytesIO(rendered.samples))
    return Image.frombytes(mode, (rendered.width_px, rendered.height_px), rendered.samples)


def _encode_webp(image, cfg: StageAConfig) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="WEBP", quality=cfg.webp_quality, method=cfg.webp_method)
    return buf.getvalue()


def _encode_png(image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


# --- construction ---------------------------------------------------------


def build_pyramid(
    backend: PdfBackend,
    geometry: PageGeometry,
    store: ObjectStore,
    cfg: StageAConfig,
) -> TilePyramidResult:
    """Render and store every tile for one page. One raster live at a time."""
    page = geometry.page_number
    plan = plan_pyramid(
        geometry.width_px,
        geometry.height_px,
        tile_size=cfg.tile_size,
        base_dpi=cfg.render_dpi,
    )

    level_entries: list[dict] = []
    keys: set[str] = set()
    bytes_written = 0
    tile_count = 0

    direct_levels = [lvl for lvl in plan.levels if lvl.source == "direct"]
    coarse_levels = [lvl for lvl in plan.levels if lvl.source == "downsampled"]

    for lvl in direct_levels:
        tiles: dict[str, str] = {}
        for row in range(lvl.rows):
            for col in range(lvl.cols):
                clip = BBox(
                    x0=float(col * cfg.tile_size),
                    y0=float(row * cfg.tile_size),
                    x1=float(min((col + 1) * cfg.tile_size, lvl.width_px)),
                    y1=float(min((row + 1) * cfg.tile_size, lvl.height_px)),
                )
                with page_pixels(backend, page, dpi=lvl.render_dpi, clip=clip) as rendered:
                    data = _encode_webp(_image_from_rendered(rendered), cfg)
                ref = store.put_bytes(data, content_type="image/webp")
                tiles[f"{col}/{row}"] = ref.key
                tile_count += 1
                if ref.key not in keys:
                    keys.add(ref.key)
                    bytes_written += ref.size_bytes
        level_entries.append(_level_entry(lvl, tiles))

    thumbnail_key: str | None = None
    # One render feeds every coarse level *and* the thumbnail.
    with page_pixels(backend, page, dpi=cfg.coarse_source_dpi) as rendered:
        source = _image_from_rendered(rendered)
        for lvl in coarse_levels:
            tiles = {}
            level_img = _resize(source, lvl.width_px, lvl.height_px)
            for row in range(lvl.rows):
                for col in range(lvl.cols):
                    box = (
                        col * cfg.tile_size,
                        row * cfg.tile_size,
                        min((col + 1) * cfg.tile_size, lvl.width_px),
                        min((row + 1) * cfg.tile_size, lvl.height_px),
                    )
                    data = _encode_webp(level_img.crop(box), cfg)
                    ref = store.put_bytes(data, content_type="image/webp")
                    tiles[f"{col}/{row}"] = ref.key
                    tile_count += 1
                    if ref.key not in keys:
                        keys.add(ref.key)
                        bytes_written += ref.size_bytes
            level_entries.append(_level_entry(lvl, tiles))
        thumb = _thumbnail(source, cfg.thumbnail_max_px)
        thumb_ref = store.put_bytes(_encode_webp(thumb, cfg), content_type="image/webp")
        thumbnail_key = thumb_ref.key
        if thumb_ref.key not in keys:
            keys.add(thumb_ref.key)
            bytes_written += thumb_ref.size_bytes

    level_entries.sort(key=lambda e: e["z"])
    manifest = {
        "schema": TILE_MANIFEST_SCHEMA,
        "page_number": page,
        "render_dpi": cfg.render_dpi,
        "rotation_deg": geometry.rotation_deg,
        "tile_size": cfg.tile_size,
        "format": "image/webp",
        "webp_quality": cfg.webp_quality,
        "base_width_px": plan.base_width_px,
        "base_height_px": plan.base_height_px,
        "max_zoom": plan.max_zoom,
        "levels": level_entries,
    }
    manifest_ref = store.put_bytes(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        content_type="application/json",
    )
    bytes_written += manifest_ref.size_bytes

    return TilePyramidResult(
        manifest_key=manifest_ref.key,
        max_zoom=plan.max_zoom,
        tile_count=tile_count,
        stored_object_count=len(keys),
        bytes_written=bytes_written,
        thumbnail_key=thumbnail_key,
    )


def _level_entry(lvl: TileLevel, tiles: dict[str, str]) -> dict:
    return {
        "z": lvl.z,
        "width_px": lvl.width_px,
        "height_px": lvl.height_px,
        "cols": lvl.cols,
        "rows": lvl.rows,
        "source": lvl.source,
        "render_dpi": lvl.render_dpi,
        "tiles": tiles,
    }


def _resize(image, width: int, height: int):
    from PIL import Image

    if (image.width, image.height) == (width, height):
        return image
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _thumbnail(image, max_px: int):
    scale = min(1.0, max_px / max(image.width, image.height))
    return _resize(image, max(1, int(image.width * scale)), max(1, int(image.height * scale)))


def render_full_page(
    backend: PdfBackend,
    geometry: PageGeometry,
    store: ObjectStore,
    cfg: StageAConfig,
) -> tuple[str, int] | None:
    """Whole-page PNG at ``render_dpi``, or ``None`` if it busts the budget.

    Returns ``(object_key, bytes_written)``. See
    ``StageAConfig.full_page_raster_max_pixels`` for why this is conditional.
    """
    if geometry.width_px * geometry.height_px > cfg.full_page_raster_max_pixels:
        return None
    with page_pixels(backend, geometry.page_number, dpi=cfg.render_dpi) as rendered:
        data = _encode_png(_image_from_rendered(rendered))
    ref = store.put_bytes(data, content_type="image/png")
    return ref.key, ref.size_bytes
