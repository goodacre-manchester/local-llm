#!/usr/bin/env python3
"""
One-off smoke test for the in-process Nemotron Parse path.

Renders a single page from a chosen PDF, runs it through Parse via the
helpers in extract-nemo.py, prints the raw + cleaned output. The goal is
to confirm Parse via HF transformers + bundled GenerationConfig does NOT
collapse into token-repeat loops the way the vLLM-served path did (see
NEXT-STEPS-NEMOTRON-EVAL.md).

Usage (run inside scripts/extract/.venv-nemo):
  python _smoke_nemo.py <pdf-path> [pages=100]
    pages may be a single number (100) or a range (100-104).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF


def _load_extract_nemo():
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("extract_nemo", here / "extract-nemo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_pages_arg(arg: str) -> list[int]:
    if "-" in arg:
        lo, hi = arg.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(arg)]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1

    pdf_path = Path(argv[0]).resolve()
    pages = _parse_pages_arg(argv[1]) if len(argv) > 1 else [100]

    if not pdf_path.is_file():
        print(f"pdf not found: {pdf_path}", file=sys.stderr)
        return 2

    en = _load_extract_nemo()

    print(f"[smoke] opening {pdf_path.name}", flush=True)
    doc = fitz.open(str(pdf_path))
    n = len(doc)

    times: list[tuple[int, float, int]] = []  # (page_no, dt, raw_len)
    last_cleaned = ""

    for page_no in pages:
        if page_no < 1 or page_no > n:
            print(f"[smoke] page {page_no} out of range (1..{n}); skipping", flush=True)
            continue
        print(f"[smoke] page {page_no}/{n}: rendering", flush=True)
        png = en._render_page_png(doc[page_no - 1])
        t0 = time.time()
        raw = en._parse_image(png)
        dt = time.time() - t0
        cleaned = en._clean_parse_md(raw)
        last_cleaned = cleaned
        print(f"[smoke] page {page_no}: generate {dt:.1f}s, raw {len(raw)} chars, cleaned {len(cleaned)} chars",
              flush=True)
        times.append((page_no, dt, len(raw)))

    if not times:
        print("[smoke] no pages processed", flush=True)
        return 3

    print("\n========== TIMING ==========")
    for p, dt, rl in times:
        print(f"  page {p:4d}  {dt:6.2f}s  raw={rl}")
    only_dt = [dt for _, dt, _ in times]
    # Exclude the first page from the average so model warmup doesn't skew it.
    warm_dt = only_dt[1:] if len(only_dt) > 1 else only_dt
    print(f"\n  avg (all)   {sum(only_dt)/len(only_dt):.2f}s")
    print(f"  avg (warm)  {sum(warm_dt)/len(warm_dt):.2f}s  (excludes first)")
    print(f"  min         {min(only_dt):.2f}s")
    print(f"  max         {max(only_dt):.2f}s")
    print(f"  -> est full {n}-page run @ warm avg: {n * (sum(warm_dt)/len(warm_dt)) / 3600:.1f}h")

    print("\n========== LAST PAGE CLEANED (first 800 chars) ==========\n")
    print(last_cleaned[:800])
    print("\n========== END ==========")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
