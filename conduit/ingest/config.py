"""Stage A knobs, in one place, with the reason for every default.

Nothing here is tuned against real plan sets — there are none yet (risk R10).
These are structural choices plus the limits this machine imposes; the numbers
that matter (throughput, peak RSS) are *measured* into ``bench/RESULTS.md``,
never predicted here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["PathsCodec", "StageAConfig"]

PathsCodec = Literal["zstd", "gzip"]

#: The backend refuses DPI below 36 (``PageGeometry`` guards ``ck_sheet_dpi``),
#: so pyramid levels coarser than that are produced by downsampling a single
#: 36 dpi page render rather than by asking for an illegal DPI.
MIN_BACKEND_DPI = 36


@dataclass(frozen=True, slots=True)
class StageAConfig:
    """Configuration for one stage A execution."""

    #: Base raster DPI. 200 is the design's number (``01-architecture.md`` §A);
    #: every ``Detection`` bbox is later expressed in pixels at this DPI, so it
    #: is recorded on both ``Sheet.render_dpi`` and ``PipelineRun.render_dpi``.
    render_dpi: int = 200

    #: Deep-zoom tile edge in pixels (``02-tech-stack.md`` §10).
    tile_size: int = 256
    webp_quality: int = 80
    #: Pillow's WebP effort knob (0 fastest .. 6 slowest). 4 measured ~3 ms per
    #: 256 px tile on this box against ~2.3 ms at 0, for ~40% fewer bytes.
    webp_method: int = 4

    #: Longest edge of the sheet-list thumbnail, in pixels.
    thumbnail_max_px: int = 300

    #: DPI of the single page render that feeds the coarse pyramid levels and
    #: the thumbnail. Kept at the backend minimum: an ARCH E sheet at 36 dpi is
    #: 1728x1296 px (~6.7 MB), which is the largest raster stage A holds for a
    #: page whose tiles are all rendered by clip.
    coarse_source_dpi: int = MIN_BACKEND_DPI

    #: Write a single whole-page PNG (``Sheet.raster_object_key``) when the page
    #: is at most this many pixels at ``render_dpi``. Above it, the max-zoom
    #: tile level is the authoritative raster and the column stays NULL: an
    #: ARCH E sheet at 200 dpi is 69 Mpx = ~207 MB of samples, which breaks the
    #: memory discipline this stage exists to keep. ARCH D at 200 dpi (34.6 Mpx)
    #: fits under the default; ARCH E does not. Deliberate, and recorded in
    #: ``bench/RESULTS.md`` as a per-run count.
    full_page_raster_max_pixels: int = 40_000_000

    #: Compression for ``paths.json``. ``zstd`` is the design's format; ``gzip``
    #: is the documented fallback when ``zstandard`` is not installed
    #: (``conduit/PROFILES.md``).
    paths_codec: PathsCodec = "zstd"
    paths_zstd_level: int = 10

    #: Turn tile generation off for a text-only re-index; the pyramid is by far
    #: the most expensive part of the stage.
    write_tiles: bool = True

    def __post_init__(self) -> None:
        if self.render_dpi < MIN_BACKEND_DPI:
            raise ValueError(f"render_dpi must be >= {MIN_BACKEND_DPI}, got {self.render_dpi}")
        if self.tile_size < 32 or self.tile_size & (self.tile_size - 1):
            raise ValueError(f"tile_size must be a power of two >= 32, got {self.tile_size}")
