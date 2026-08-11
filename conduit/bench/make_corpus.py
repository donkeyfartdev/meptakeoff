"""Generate the SYNTHETIC test plan set.

    python -m bench.make_corpus            # 24 pages -> bench/out/synthetic_corpus.pdf
    python -m conduit.bench.make_corpus --pages 48 --out /tmp/big.pdf

What it produces, and why each part exists
------------------------------------------
* **Varied ``/Rotate``** (0, 90, 180, 270) — the rotation cases are where
  coordinate transforms break, and ``tests/test_geometry.py`` round-trips every
  page of this file.
* **Varied MediaBox** — ARCH D (36x24 in) and ARCH E (48x36 in) landscape, so
  page size is never assumed.
* **Vector pages** — real text spans (title block, legend, notes, tags) and
  real vector paths (single- and multi-segment runs, a hatched area, a leader
  line), so ``text_spans()`` and ``drawings()`` have something to return.
* **Flattened-raster pages** — a low-DPI grayscale image of a vector page with
  no text layer at all. These are the "no vector text, OCR needed" case, which
  week 1 records and skips.
* **Title block** with a sheet number and title, in the bottom-right corner,
  including one page whose title block is rotated 90 degrees.
* **Legend block** listing a few symbol/description pairs.
* **2 deliberately corrupt pages** — one with ``/Contents`` pointing at a
  non-existent object, one whose content stream has been replaced with
  garbage. They exist so the pipeline's failure path is exercised: a corrupt
  page must raise a typed ``CorruptPageError`` and be recorded, not take the
  run down.

The page count defaults to 24 rather than the roadmap's 200 because ``/home``
on the current machine is a 300 MB volume; ``--pages 200`` still works if you
have the disk. Nothing about the corpus is realistic MEP content — see
``bench/CORPUS.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import pymupdf  # bench tooling may import PyMuPDF directly; pipeline code may not

# Page sizes in points (72/in), landscape, as plan sheets are drawn.
ARCH_D = (36 * 72.0, 24 * 72.0)
ARCH_E = (48 * 72.0, 36 * 72.0)

DISCIPLINE_SHEETS = [
    ("E-101", "ELECTRICAL - LIGHTING PLAN - LEVEL 1"),
    ("E-102", "ELECTRICAL - POWER PLAN - LEVEL 1"),
    ("E-201", "ELECTRICAL - PANEL SCHEDULES"),
    ("P-101", "PLUMBING - WASTE AND VENT PLAN"),
    ("P-102", "PLUMBING - DOMESTIC WATER PLAN"),
    ("M-101", "MECHANICAL - HVAC PLAN - LEVEL 1"),
    ("M-102", "MECHANICAL - DUCTWORK PLAN - LEVEL 2"),
    ("M-501", "MECHANICAL - EQUIPMENT SCHEDULE"),
]

LEGEND_ROWS = [
    ("2x4 RECESSED LUMINAIRE", "TYPE A"),
    ("DUPLEX RECEPTACLE", "R1"),
    ("JUNCTION BOX", "J"),
    ("SUPPLY DIFFUSER", "SD-1"),
    ("FLOOR DRAIN", "FD-2"),
]

ROTATIONS = [0, 90, 0, 270, 0, 180]


@dataclass
class PageSpec:
    page_number: int
    kind: str  # "vector" | "raster" | "corrupt_missing_contents" | "corrupt_garbage_stream"
    width_pt: float
    height_pt: float
    rotation: int
    sheet_number: str
    sheet_title: str


@dataclass
class CorpusManifest:
    """Ground truth for the *structure* of the corpus — never for accuracy."""

    generator: str
    pymupdf_version: str
    page_count: int
    sha256: str
    size_bytes: int
    pages: list[dict]
    corrupt_pages: list[int]
    raster_pages: list[int]
    note: str = (
        "SYNTHETIC corpus. Structural ground truth only (page size, rotation, "
        "vector-vs-raster, corrupt pages). No detection or measurement accuracy "
        "number may be derived from this file."
    )


def plan_pages(n_pages: int) -> list[PageSpec]:
    specs: list[PageSpec] = []
    for i in range(n_pages):
        num, title = DISCIPLINE_SHEETS[i % len(DISCIPLINE_SHEETS)]
        size = ARCH_D if (i // 2) % 2 == 0 else ARCH_E
        rot = ROTATIONS[i % len(ROTATIONS)]
        kind = "raster" if i % 5 == 4 else "vector"
        specs.append(
            PageSpec(
                page_number=i + 1,
                kind=kind,
                width_pt=size[0],
                height_pt=size[1],
                rotation=rot,
                sheet_number=f"{num}" if i < len(DISCIPLINE_SHEETS) else f"{num}.{i // 8}",
                sheet_title=title,
            )
        )
    # Two deliberately corrupt pages, placed off the ends so the good pages
    # around them keep their variety. Never page 1 (that would be a
    # document-level failure, which is a different test).
    if n_pages >= 6:
        specs[n_pages // 3].kind = "corrupt_missing_contents"
        specs[(2 * n_pages) // 3].kind = "corrupt_garbage_stream"
    return specs


# --- drawing helpers ------------------------------------------------------
# All of these draw in MuPDF page space (y down, origin top-left), which is the
# space PyMuPDF's writer uses. The library under test converts to pdf_points.


def _title_block(page: pymupdf.Page, spec: PageSpec, *, rotate: int = 0) -> None:
    w, h = page.rect.width, page.rect.height
    bw, bh = 320.0, 110.0
    rect = pymupdf.Rect(w - bw - 24, h - bh - 24, w - 24, h - 24)
    page.draw_rect(rect, color=(0, 0, 0), width=1.5)
    page.draw_line(
        pymupdf.Point(rect.x0, rect.y0 + 34), pymupdf.Point(rect.x1, rect.y0 + 34), width=0.8
    )
    page.insert_textbox(
        pymupdf.Rect(rect.x0 + 8, rect.y0 + 6, rect.x1 - 8, rect.y0 + 32),
        "CONDUIT SYNTHETIC PROJECT\nISSUED FOR BID - NOT A REAL PROJECT",
        fontsize=8,
        fontname="helv",
        rotate=rotate,
    )
    page.insert_textbox(
        pymupdf.Rect(rect.x0 + 8, rect.y0 + 40, rect.x1 - 8, rect.y1 - 30),
        spec.sheet_title,
        fontsize=10,
        fontname="hebo",
        rotate=rotate,
    )
    page.insert_textbox(
        pymupdf.Rect(rect.x0 + 8, rect.y1 - 28, rect.x1 - 8, rect.y1 - 6),
        f"SHEET NUMBER:  {spec.sheet_number}",
        fontsize=12,
        fontname="hebo",
        rotate=rotate,
    )


def _legend(page: pymupdf.Page) -> None:
    x0, y0 = 40.0, 40.0
    rect = pymupdf.Rect(x0, y0, x0 + 260, y0 + 30 + 18 * len(LEGEND_ROWS))
    page.draw_rect(rect, color=(0, 0, 0), width=1.0)
    page.insert_text(pymupdf.Point(x0 + 8, y0 + 18), "LEGEND", fontsize=11, fontname="hebo")
    y = y0 + 38
    for desc, tag in LEGEND_ROWS:
        page.draw_rect(pymupdf.Rect(x0 + 8, y - 8, x0 + 20, y + 4), color=(0, 0, 0), width=0.8)
        page.insert_text(pymupdf.Point(x0 + 28, y), f"{tag}  {desc}", fontsize=7, fontname="helv")
        y += 18


def _plan_content(page: pymupdf.Page, spec: PageSpec) -> None:
    w, h = page.rect.width, page.rect.height
    # Border
    page.draw_rect(pymupdf.Rect(18, 18, w - 18, h - 18), color=(0, 0, 0), width=2.0)
    # A grid of "rooms"
    for gx in range(2, 8):
        x = 60 + gx * 120.0
        if x < w - 380:
            page.draw_line(pymupdf.Point(x, 200), pymupdf.Point(x, h - 200), width=0.4)
    for gy in range(1, 5):
        y = 200 + gy * 120.0
        if y < h - 200:
            page.draw_line(pymupdf.Point(300, y), pymupdf.Point(w - 380, y), width=0.4)
    # Polyline "runs" — the thing stage E will trace.
    run = [(320.0, 260.0), (720.0, 260.0), (720.0, 520.0), (1080.0, 520.0)]
    for a, b in zip(run, run[1:], strict=False):
        page.draw_line(pymupdf.Point(*a), pymupdf.Point(*b), width=1.6)
    # A dashed run
    page.draw_line(
        pymupdf.Point(320.0, 320.0), pymupdf.Point(980.0, 320.0), width=1.2, dashes="[3 3] 0"
    )
    # A leader line + tag, which stage E must NOT treat as a run
    page.draw_line(pymupdf.Point(760.0, 300.0), pymupdf.Point(820.0, 250.0), width=0.5)
    page.insert_text(pymupdf.Point(822.0, 248.0), "TYPE A (4)", fontsize=7, fontname="helv")
    # Fixture-ish symbols
    for i in range(6):
        cx, cy = 380.0 + i * 90.0, 420.0
        page.draw_rect(pymupdf.Rect(cx - 12, cy - 6, cx + 12, cy + 6), width=0.8)
        page.insert_text(pymupdf.Point(cx - 10, cy + 18), "A", fontsize=6, fontname="helv")
    # Scale note and a dimension string
    page.insert_text(
        pymupdf.Point(300.0, h - 120), 'SCALE: 1/8" = 1\'-0"', fontsize=9, fontname="hebo"
    )
    page.insert_text(pymupdf.Point(300.0, h - 100), "24'-6\"", fontsize=8, fontname="helv")
    page.insert_text(
        pymupdf.Point(40.0, h - 60),
        "GENERAL NOTE: SYNTHETIC SHEET GENERATED FOR PIPELINE TESTING.",
        fontsize=7,
        fontname="helv",
    )


def _render_flat(doc: pymupdf.Document, spec: PageSpec, dpi: int) -> pymupdf.Page:
    """Build a vector page in a scratch doc, rasterise it, place the image.

    The result has zero text spans: the "flattened scan" case.
    """
    scratch = pymupdf.open()
    sp = scratch.new_page(width=spec.width_pt, height=spec.height_pt)
    _plan_content(sp, spec)
    _legend(sp)
    _title_block(sp, spec)
    pix = sp.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    img = pix.tobytes("png")
    del pix
    scratch.close()
    page = doc.new_page(width=spec.width_pt, height=spec.height_pt)
    page.insert_image(page.rect, stream=img)
    return page


def build(out_path: Path, n_pages: int, *, raster_dpi: int = 50) -> CorpusManifest:
    specs = plan_pages(n_pages)
    doc = pymupdf.open()
    for spec in specs:
        if spec.kind == "raster":
            page = _render_flat(doc, spec, raster_dpi)
        else:
            page = doc.new_page(width=spec.width_pt, height=spec.height_pt)
            _plan_content(page, spec)
            _legend(page)
            # One flavour of page gets a rotated title block, because real
            # title blocks on rotated sheets are often set at 90 degrees.
            _title_block(page, spec, rotate=90 if spec.page_number % 7 == 0 else 0)
        if spec.rotation:
            page.set_rotation(spec.rotation)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path), garbage=3, deflate=True)
    doc.close()

    corrupt = [s.page_number for s in specs if s.kind.startswith("corrupt")]
    if corrupt:
        _corrupt_pages(out_path, specs)

    data = out_path.read_bytes()
    return CorpusManifest(
        generator="conduit.bench.make_corpus",
        pymupdf_version=pymupdf.version[0],
        page_count=len(specs),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        pages=[asdict(s) for s in specs],
        corrupt_pages=corrupt,
        raster_pages=[s.page_number for s in specs if s.kind == "raster"],
    )


def _corrupt_pages(path: Path, specs: list[PageSpec]) -> None:
    """Damage the two designated pages in the saved file, in place.

    Done after saving because PyMuPDF's writer would repair the damage.
    """
    doc = pymupdf.open(str(path))
    for spec in specs:
        if not spec.kind.startswith("corrupt"):
            continue
        page = doc.load_page(spec.page_number - 1)
        xref = page.xref
        if spec.kind == "corrupt_missing_contents":
            # /Contents points at an object that does not exist in the xref.
            doc.xref_set_key(xref, "Contents", "99999 0 R")
        elif spec.kind == "corrupt_garbage_stream":
            contents = page.get_contents()
            for cxref in contents:
                doc.update_stream(cxref, b"q 1 0 0 1 0 0 cm BT /BADFONT Tf ( ) Tj 99 99 zz\n")
    doc.save(str(path), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    doc.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the synthetic plan-set corpus.")
    ap.add_argument("--pages", type=int, default=24, help="page count (default 24)")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("bench/out/synthetic_corpus.pdf"),
        help="output PDF path (default bench/out/synthetic_corpus.pdf)",
    )
    ap.add_argument("--raster-dpi", type=int, default=50, help="DPI for flattened pages")
    args = ap.parse_args(argv)

    if args.pages < 6:
        ap.error("--pages must be at least 6 (the corrupt-page slots need room)")

    manifest = build(args.out, args.pages, raster_dpi=args.raster_dpi)
    manifest_path = args.out.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(asdict(manifest), indent=2) + "\n")
    print(
        f"wrote {args.out} ({manifest.size_bytes / 1e6:.2f} MB, {manifest.page_count} pages)\n"
        f"  sha256          {manifest.sha256}\n"
        f"  raster pages    {manifest.raster_pages}\n"
        f"  corrupt pages   {manifest.corrupt_pages}\n"
        f"  manifest        {manifest_path}\n"
        "  SYNTHETIC — structural test input only, no accuracy number may come from it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
