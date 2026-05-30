#!/usr/bin/env python3
"""
Build a human review page for every picture block in a collection.

Usage:
    python build-picture-review.py <data_dir> [collection]

For each collection it emits a single HTML file:
    data/<collection>/picture-review.html

Layout: each source PDF is a collapsible <details> section. Inside, every
type:"picture" block is a row showing the persisted PNG alongside its
figure caption, VLM description (stripped), and raw VLM output (collapsed
by default). PNGs are referenced relatively (no base64) so the file stays
small and reflects updates from rerender-pictures.py automatically.

Open the file from the file:// protocol or any static-file server.

Per-PDF section header reports counts: total / captioned / empty / missing-PNG.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path


_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; padding: 24px; max-width: 1600px; background: #fafafa; color: #111; }
h1 { margin-top: 0; }
.intro { color: #555; margin-bottom: 24px; }
details { background: #fff; border: 1px solid #ddd; border-radius: 6px;
          margin-bottom: 12px; padding: 0 16px; }
details > summary { padding: 12px 0; cursor: pointer; font-weight: 600; }
details[open] > summary { border-bottom: 1px solid #eee; margin-bottom: 12px; }
.fig { display: flex; gap: 16px; padding: 16px 0; border-bottom: 1px solid #f0f0f0; }
.fig:last-child { border-bottom: none; }
.fig img { max-width: 520px; max-height: 520px; object-fit: contain;
           background: #fff; border: 1px solid #ddd; align-self: flex-start; }
.fig .meta { flex: 1; min-width: 0; }
.fig h3 { margin: 0 0 4px 0; font-size: 14px; color: #333; }
.fig .caption { margin: 4px 0; font-weight: 600; color: #222; }
.fig .empty   { color: #b00; font-style: italic; }
.fig .vlm     { margin: 8px 0; white-space: pre-wrap; line-height: 1.4; font-size: 14px; }
.fig details.raw { margin-top: 8px; background: #f6f6f6; border-radius: 4px;
                   padding: 0 8px; border: 1px solid #e3e3e3; }
.fig details.raw summary { padding: 6px 0; font-size: 13px; color: #555; cursor: pointer; }
.fig details.raw pre { white-space: pre-wrap; font-size: 12px; margin: 8px 0; color: #333; }
.counts { font-weight: 400; color: #666; font-size: 13px; }
.missing { color: #b00; }
"""


def _render_pdf_section(pdf_stem: str, pics: list[dict],
                        collection_dir: Path) -> tuple[str, dict]:
    total = len(pics)
    captioned = 0
    empty = 0
    missing = 0

    rows: list[str] = []
    for pic in pics:
        pid = pic.get("id") or ""
        page = pic.get("page", "?")
        section = (pic.get("section") or "").strip()
        caption = (pic.get("caption") or "").strip()
        vlm = (pic.get("vlm_description") or "").strip()
        vlm_raw = (pic.get("vlm_description_raw") or "").strip()
        img_rel = (pic.get("image_path") or "").strip()

        img_ok = bool(img_rel) and (collection_dir / img_rel).exists()
        if vlm:
            captioned += 1
        else:
            empty += 1
        if not img_ok:
            missing += 1

        if img_ok:
            img_html = f'<img src="{html.escape(img_rel)}" loading="lazy" alt="{html.escape(pid)}">'
        else:
            img_html = f'<div class="missing">[missing PNG: {html.escape(img_rel or "(no image_path)")}]</div>'

        caption_html = (f'<p class="caption">{html.escape(caption)}</p>'
                        if caption else
                        '<p class="caption empty">(no caption detected)</p>')

        if vlm:
            vlm_html = f'<p class="vlm">{html.escape(vlm)}</p>'
        else:
            vlm_html = '<p class="vlm empty">(not yet captioned)</p>'

        raw_html = ""
        if vlm_raw and vlm_raw != vlm:
            raw_html = (
                f'<details class="raw"><summary>raw VLM output</summary>'
                f'<pre>{html.escape(vlm_raw)}</pre></details>'
            )

        section_html = (f'<div class="section">section: {html.escape(section)}</div>'
                        if section else "")

        rows.append(
            f'<div class="fig" id="{html.escape(pid)}">'
            f'  {img_html}'
            f'  <div class="meta">'
            f'    <h3>{html.escape(pid)} — page {page}</h3>'
            f'    {section_html}'
            f'    {caption_html}'
            f'    {vlm_html}'
            f'    {raw_html}'
            f'  </div>'
            f'</div>'
        )

    summary_bits = [f"{total} pics"]
    if captioned:
        summary_bits.append(f"{captioned} captioned")
    if empty:
        summary_bits.append(f"{empty} empty")
    if missing:
        summary_bits.append(f"{missing} missing PNG")
    counts_html = ", ".join(summary_bits)

    section = (
        f'<details>'
        f'<summary>{html.escape(pdf_stem)} '
        f'<span class="counts">({counts_html})</span></summary>'
        f'{"".join(rows)}'
        f'</details>'
    )
    return section, {"total": total, "captioned": captioned,
                     "empty": empty, "missing": missing}


def build_collection(collection_dir: Path) -> Path | None:
    cache = collection_dir / ".rag-cache"
    if not cache.is_dir():
        return None
    sidecars = sorted(cache.glob("*.json"))
    if not sidecars:
        return None

    sections: list[str] = []
    totals = {"total": 0, "captioned": 0, "empty": 0, "missing": 0}
    pdf_count = 0
    for sc in sidecars:
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ! {sc.name}: bad JSON: {exc}")
            continue
        pics = [b for b in data.get("blocks", []) if b.get("type") == "picture"]
        if not pics:
            continue
        pdf_stem = sc.name.rsplit(".pdf.json", 1)[0]
        section_html, stats = _render_pdf_section(pdf_stem, pics, collection_dir)
        sections.append(section_html)
        pdf_count += 1
        for k, v in stats.items():
            totals[k] += v

    if not sections:
        return None

    intro = (
        f'<h1>{html.escape(collection_dir.name)} — picture review</h1>'
        f'<p class="intro">{pdf_count} PDFs, '
        f'{totals["total"]} picture blocks, '
        f'{totals["captioned"]} captioned, '
        f'{totals["empty"]} not yet captioned, '
        f'{totals["missing"]} missing PNG.</p>'
    )

    html_doc = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<title>{html.escape(collection_dir.name)} — picture review</title>'
        f'<style>{_CSS}</style></head><body>'
        f'{intro}{"".join(sections)}</body></html>'
    )

    out = collection_dir / "picture-review.html"
    out.write_text(html_doc, encoding="utf-8")
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    data_dir = Path(sys.argv[1]).resolve()
    if not data_dir.is_dir():
        print(f"! data_dir does not exist: {data_dir}")
        return 2

    only = sys.argv[2] if len(sys.argv) > 2 else None
    if only:
        collections = [data_dir / only]
    else:
        collections = sorted(p for p in data_dir.iterdir()
                             if p.is_dir() and (p / ".rag-cache").is_dir())

    for col in collections:
        out = build_collection(col)
        if out:
            print(f"{col.name} -> {out}")
        else:
            print(f"{col.name}: skipped (no picture blocks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
