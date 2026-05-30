#!/usr/bin/env python3
"""
Re-render persisted picture PNGs from stored sidecar bboxes.

When BBOX_PAD_FRAC (the render-time padding around the detected picture
bbox) changes, only the PNG output changes — the stored bbox in the
sidecar is the unpadded detected extent and stays correct. So we don't
need to re-run the full extractor (Parse takes ~21h for IEEE); we walk
each sidecar, open the source PDF once, and re-render every
type:"picture" block's PNG in place.

Usage:
    python rerender-pictures.py <data_dir> [collection] [--x-pad F] [--y-pad F]

Args:
    data_dir   The folder containing per-collection subdirs (e.g. d:/Projects/local-llm/data).
    collection Optional collection name. If omitted, processes every subdir
               of data_dir that has a .rag-cache directory.
    --x-pad    Per-axis X padding as fraction of page long-side. Default depends
               on collection (auto-routes by sidecar `backend` field):
                  nemotron-parse-v1.2 → 0.02
                  any other backend   → 0.02
    --y-pad    Per-axis Y padding fraction. Default by backend:
                  nemotron-parse-v1.2 → 0.00 (Parse bbox already grabs
                                              vertical context)
                  any other backend   → 0.01 (PyMuPDF rect hugs raster
                                              tight on both axes)
    If --x-pad / --y-pad are given, they override the per-backend defaults
    for all sidecars in this run.

Env:
    RERENDER_DRY    set to "1" to print actions without writing PNGs
    RERENDER_LIMIT  per-file picture-block cap (for sampling). 0 = no cap.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import fitz  # PyMuPDF


TARGET_PX = int(os.environ.get("RERENDER_TARGET_PX", "2048"))
DRY = os.environ.get("RERENDER_DRY", "") == "1"
LIMIT = int(os.environ.get("RERENDER_LIMIT", "0") or 0)

# Per-backend default padding. The Parse path detects bboxes that already
# include surrounding vertical context, so y_pad=0 (else captions pick up
# adjacent paragraph text). The PyMuPDF path returns the raster image's
# tight rect, so y_pad>0 for breathing room.
_BACKEND_DEFAULTS = {
    "nemotron-parse-v1.2": (0.02, 0.00),  # x_pad, y_pad
    # fallback for any other backend (pymupdf4llm, pypdf, plain-text-pdf, etc.)
    "_default":            (0.02, 0.01),
}


def _rasterise(page: fitz.Page, bbox: list[float],
               x_pad: float, y_pad: float, target_long_side: int) -> bytes:
    r = page.rect
    longer = max(r.width, r.height)
    if longer <= 0:
        return b""
    zoom = target_long_side / longer
    x1, y1, x2, y2 = bbox
    pad_x_pt = x_pad * longer
    pad_y_pt = y_pad * longer
    cx1 = max(0.0, x1 * r.width - pad_x_pt)
    cy1 = max(0.0, y1 * r.height - pad_y_pt)
    cx2 = min(r.width, x2 * r.width + pad_x_pt)
    cy2 = min(r.height, y2 * r.height + pad_y_pt)
    clip = fitz.Rect(cx1, cy1, cx2, cy2)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          clip=clip, alpha=False)
    return pix.tobytes("png")


def _pad_for(backend: str, override: tuple[float, float] | None) -> tuple[float, float]:
    if override is not None:
        return override
    return _BACKEND_DEFAULTS.get(backend, _BACKEND_DEFAULTS["_default"])


def _process_sidecar(sidecar_path: Path, collection_dir: Path,
                     pad_override: tuple[float, float] | None
                     ) -> tuple[int, int]:
    """Returns (n_rendered, n_skipped)."""
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  ! {sidecar_path.name}: failed to parse JSON: {exc}")
        return 0, 0

    blocks = data.get("blocks", [])
    pics = [b for b in blocks if b.get("type") == "picture"]
    if not pics:
        return 0, 0

    backend = (data.get("backend") or "").strip()
    x_pad, y_pad = _pad_for(backend, pad_override)

    pdf_name = sidecar_path.name.rsplit(".json", 1)[0]
    pdf_path = collection_dir / pdf_name
    if not pdf_path.exists():
        print(f"  ! {sidecar_path.name}: source PDF missing at {pdf_path}")
        return 0, 0

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        print(f"  ! {pdf_name}: fitz.open failed: {exc}")
        return 0, 0

    n_rendered = 0
    n_skipped = 0
    for pic in pics:
        if LIMIT and n_rendered >= LIMIT:
            break
        img_rel = (pic.get("image_path") or "").strip()
        bbox = pic.get("bbox")
        page_no = pic.get("page")
        if not img_rel or not bbox or not page_no:
            n_skipped += 1
            continue
        page_idx = int(page_no) - 1
        if page_idx < 0 or page_idx >= len(doc):
            n_skipped += 1
            continue
        img_abs = collection_dir / img_rel
        try:
            png_bytes = _rasterise(doc[page_idx], bbox, x_pad, y_pad, TARGET_PX)
        except Exception as exc:
            print(f"  ! {pdf_name} {pic.get('id')}: rasterise failed: {exc}")
            n_skipped += 1
            continue
        if not png_bytes:
            n_skipped += 1
            continue
        if DRY:
            print(f"  ~ would write {img_abs} ({len(png_bytes)} bytes)")
        else:
            img_abs.parent.mkdir(parents=True, exist_ok=True)
            img_abs.write_bytes(png_bytes)
        n_rendered += 1
        if n_rendered % 100 == 0:
            print(f"    {pdf_name}: {n_rendered} pics rerendered "
                  f"(backend={backend}, x={x_pad}, y={y_pad})", flush=True)

    doc.close()
    return n_rendered, n_skipped


def _parse_pad_args(argv: list[str]) -> tuple[list[str], tuple[float, float] | None]:
    """Strip --x-pad / --y-pad from argv. Returns (cleaned_argv, override or None).
    If only one is given, the other defaults to 0.0."""
    x_pad: float | None = None
    y_pad: float | None = None
    out: list[str] = []
    it = iter(argv)
    for tok in it:
        if tok == "--x-pad":
            x_pad = float(next(it))
        elif tok.startswith("--x-pad="):
            x_pad = float(tok.split("=", 1)[1])
        elif tok == "--y-pad":
            y_pad = float(next(it))
        elif tok.startswith("--y-pad="):
            y_pad = float(tok.split("=", 1)[1])
        else:
            out.append(tok)
    if x_pad is None and y_pad is None:
        return out, None
    return out, (x_pad or 0.0, y_pad or 0.0)


def main() -> int:
    argv, pad_override = _parse_pad_args(sys.argv[1:])
    if not argv:
        print(__doc__)
        return 2

    data_dir = Path(argv[0]).resolve()
    if not data_dir.is_dir():
        print(f"! data_dir does not exist: {data_dir}")
        return 2

    only_collection = argv[1] if len(argv) > 1 else None

    collections: list[Path]
    if only_collection:
        collections = [data_dir / only_collection]
    else:
        collections = sorted(p for p in data_dir.iterdir()
                             if p.is_dir() and (p / ".rag-cache").is_dir())

    total_rendered = 0
    total_skipped = 0
    if pad_override:
        print(f"pad override: x={pad_override[0]} y={pad_override[1]}")
    else:
        print(f"pad defaults (per backend): {_BACKEND_DEFAULTS}")
    print(f"TARGET_PX={TARGET_PX}  DRY={'YES' if DRY else 'no'}  "
          f"LIMIT={LIMIT or 'none'}")
    for col_dir in collections:
        cache = col_dir / ".rag-cache"
        if not cache.is_dir():
            print(f"skip {col_dir.name}: no .rag-cache")
            continue
        print(f"\n== {col_dir.name} ==")
        for sidecar in sorted(cache.glob("*.json")):
            n_r, n_s = _process_sidecar(sidecar, col_dir, pad_override)
            if n_r or n_s:
                print(f"  {sidecar.name}: rerendered={n_r} skipped={n_s}")
            total_rendered += n_r
            total_skipped += n_s

    print(f"\nDONE  total_rendered={total_rendered}  total_skipped={total_skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
