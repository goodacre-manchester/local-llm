#!/usr/bin/env python3
"""
PDF -> page-tagged JSON sidecar extractor for the local-llm RAG pipeline.

For every PDF under <data_dir>/<collection>/ this writes a sidecar at
<data_dir>/<collection>/.rag-cache/<pdf>.json with the schema:

    {
      "doc": "ug1399-vitis-hls.pdf",
      "source_mtime": "2026-05-18T08:11:18.000Z",
      "backend": "docling" | "pymupdf4llm" | "pypdf",
      "blocks": [
        {"id": "p12-b3", "page": 12, "section": "3 Scheduling",
         "type": "text" | "table" | "heading", "text": "..."}
      ]
    }

The RAG server consumes this instead of doing flat text extraction, so
chunks keep page numbers, section context, and intact tables for citations.

Backend is chosen by availability, best first:
    docling        - layout + table-structure model (best for datasheets)
    pymupdf4llm    - pip-only, no ML, page-tagged Markdown + basic tables
    pypdf          - last-resort plain text, one block per page

Unchanged PDFs (same mtime as recorded in the sidecar) are skipped unless
--force is given.

Usage:
    python extract.py <data_dir> [collection] [--force]
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path


def _iso_mtime(p: Path) -> str:
    # Mirror JS new Date(mtimeMs).toISOString() so the server's skip check lines up.
    ts = _dt.datetime.fromtimestamp(p.stat().st_mtime, tz=_dt.timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


# ─── Backend detection ────────────────────────────────────────────────────────

def _pick_backend() -> str:
    try:
        import docling  # noqa: F401
        return "docling"
    except Exception:
        pass
    try:
        import pymupdf4llm  # noqa: F401
        return "pymupdf4llm"
    except Exception:
        pass
    try:
        import pypdf  # noqa: F401
        return "pypdf"
    except Exception:
        sys.exit(
            "No extraction backend available. Install one of:\n"
            "  pip install docling          # best quality\n"
            "  pip install pymupdf4llm      # lightweight, recommended minimum\n"
            "  pip install pypdf            # bare fallback"
        )


# ─── Markdown -> blocks (shared by pymupdf4llm) ───────────────────────────────

def _md_page_to_blocks(md: str, page: int, section_state: dict) -> list[dict]:
    """Split one page of Markdown into heading/table/text blocks."""
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    b = 0

    def emit(btype: str, text: str):
        nonlocal b
        text = text.strip()
        if not text:
            return
        blocks.append({
            "id": f"p{page}-b{b}",
            "page": page,
            "section": section_state.get("current", ""),
            "type": btype,
            "text": text,
        })
        b += 1

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            section_state["current"] = heading
            emit("heading", heading)
            i += 1
            continue

        # Markdown table: consecutive lines containing a pipe. Kept as ONE block
        # so the server never splits a table across chunks.
        if "|" in stripped and stripped:
            tbl = []
            while i < len(lines) and "|" in lines[i]:
                tbl.append(lines[i])
                i += 1
            emit("table", "\n".join(tbl))
            continue

        # Paragraph: accumulate until a blank line.
        para = []
        while i < len(lines) and lines[i].strip():
            para.append(lines[i].strip())
            i += 1
        emit("text", " ".join(para))
        i += 1  # skip the blank line

    return blocks


# ─── PDF outline / bookmark → authoritative clause path ──────────────────────

def _build_toc(pdf: Path):
    """
    Build a page → clause-path resolver from the PDF bookmark tree.

    Professional standards/datasheets carry a rich outline with exact clause
    numbers/titles and page destinations (e.g. "12.29.1 The Gate Parameter
    Table"). That is a far cleaner section signal than heuristic heading
    detection, and — critically — it separates mechanisms that collide in
    prose (IEEE 802.1Q TAS=12.29 vs PSFP=12.31 vs ATS=8.6.5.6/47).

    Returns (leaf_fn, path_fn) where, for a 1-based PDF page:
      leaf_fn(page) -> deepest active bookmark title  (used as `section`)
      path_fn(page) -> "L1 > … > leaf" breadcrumb      (kept as `section_path`)
    Returns (None, None) if the PDF has no usable outline.
    """
    try:
        import fitz  # PyMuPDF (ships with pymupdf4llm)
        toc = fitz.open(str(pdf)).get_toc(simple=True)  # [[level, title, page], ...]
    except Exception:
        return None, None
    if not toc:
        return None, None

    # Walk in document order, tracking the title stack per level, and record
    # (start_page, leaf_title, breadcrumb) for each entry.
    entries = []  # (page, leaf, path)
    stack: list[tuple[int, str]] = []
    for level, title, page in toc:
        title = " ".join(str(title).split())
        if not title or page < 1:
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " > ".join(t for _, t in stack)
        entries.append((page, title, path))
    if not entries:
        return None, None
    # Degenerate-outline guard: some PDFs ship only a title-page bookmark (or a
    # couple of cover-matter entries) with no real clause tree — e.g. the CXL
    # 4.0 eval copy carries one L1 title bookmark for all 1276 pages. Applying
    # that as `section` collapses the whole document to one flat heading and
    # wipes the backend's heuristic clause headings. If the outline yields fewer
    # than 3 distinct section anchors it is not usable for sectioning; treat the
    # PDF as un-bookmarked so the heuristic headings survive. (Remediate an
    # already-extracted sidecar with resection-from-headings.py.)
    if len({leaf for _, leaf, _ in entries}) < 3:
        return None, None
    entries.sort(key=lambda e: e[0])
    start_pages = [e[0] for e in entries]

    import bisect

    def _resolve(page: int):
        # Last entry whose start page <= this page (front matter → none).
        i = bisect.bisect_right(start_pages, page) - 1
        return entries[i] if i >= 0 else None

    def leaf_fn(page: int) -> str:
        e = _resolve(page)
        return e[1] if e else ""

    def path_fn(page: int) -> str:
        e = _resolve(page)
        return e[2] if e else ""

    return leaf_fn, path_fn


def _apply_toc(pdf: Path, blocks: list[dict]) -> list[dict]:
    """Override each block's `section` with the authoritative clause from the
    bookmark tree (keep the backend's heuristic only where the outline is
    silent, e.g. front matter / un-bookmarked PDFs)."""
    leaf_fn, path_fn = _build_toc(pdf)
    if leaf_fn is None:
        return blocks
    used = 0
    for b in blocks:
        clause = leaf_fn(b.get("page") or 0)
        if clause:
            b["section"] = clause
            b["section_path"] = path_fn(b.get("page") or 0)
            used += 1
    print(f"    (clause paths from {len(blocks) and used} bookmarked blocks)", flush=True)
    return blocks


# ─── Backends ─────────────────────────────────────────────────────────────────

def _extract_pymupdf4llm(pdf: Path) -> list[dict]:
    import pymupdf4llm
    pages = pymupdf4llm.to_markdown(str(pdf), page_chunks=True)
    section_state = {"current": ""}
    blocks: list[dict] = []
    for idx, page in enumerate(pages):
        page_no = (page.get("metadata") or {}).get("page", idx + 1)
        blocks.extend(_md_page_to_blocks(page.get("text", ""), page_no, section_state))
    return blocks


def _extract_pypdf(pdf: Path) -> list[dict]:
    from pypdf import PdfReader
    reader = PdfReader(str(pdf))
    blocks: list[dict] = []
    for idx, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if text:
            blocks.append({
                "id": f"p{idx + 1}-b0",
                "page": idx + 1,
                "section": "",
                "type": "text",
                "text": text,
            })
    return blocks


def _extract_docling(pdf: Path) -> list[dict]:
    from docling.document_converter import DocumentConverter
    doc = DocumentConverter().convert(str(pdf)).document
    blocks: list[dict] = []
    section = ""
    counters: dict[int, int] = {}

    def page_of(item) -> int:
        prov = getattr(item, "prov", None) or []
        if prov and getattr(prov[0], "page_no", None):
            return prov[0].page_no
        return 0

    for item, _level in doc.iterate_items():
        label = str(getattr(item, "label", "")).lower()
        page = page_of(item)
        counters[page] = counters.get(page, -1) + 1
        bid = f"p{page}-b{counters[page]}"

        # Tables: export to Markdown, keep as a single block.
        if "table" in label or item.__class__.__name__ == "TableItem":
            try:
                text = item.export_to_markdown()
            except Exception:
                text = getattr(item, "text", "") or ""
            if text.strip():
                blocks.append({"id": bid, "page": page, "section": section,
                               "type": "table", "text": text.strip()})
            continue

        text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue

        if "title" in label or "section_header" in label or "header" in label:
            section = text
            blocks.append({"id": bid, "page": page, "section": section,
                           "type": "heading", "text": text})
        else:
            blocks.append({"id": bid, "page": page, "section": section,
                           "type": "text", "text": text})
    return blocks


# ─── Plain-text-PDF extractor (IETF RFCs and similar) ────────────────────────
#
# RFCs are published as monospace plain text rendered to PDF. pymupdf4llm
# applies markdown conversion that COLLAPSES line breaks (everything is in
# fixed-width font, so it can't tell prose from ASCII art) and wraps every
# paragraph in ``` fences (because fixed-width = code in its heuristic).
# Both behaviours are wrong for RFCs: prose paragraphs need natural flow,
# ASCII-art packet diagrams and questionnaire tables need their alignment
# preserved.
#
# This extractor uses PyMuPDF's geometric-blocks mode (`get_text("blocks")`)
# which segments the page into logical paragraphs while preserving internal
# line breaks. For each block we then:
#   - Skip top/bottom-of-page single-line chrome (RFC running header +
#     "[Page N]" footer).
#   - Detect ASCII tables and packet-format diagrams (lines containing `|`
#     or `+--` separators) and emit them as type:"code" so the renderer
#     wraps them in a fence + preserves their alignment.
#   - For prose blocks, collapse the ~72-col hard wraps into one line per
#     logical paragraph so the .md reads naturally.

_RFC_PAGE_FOOTER = re.compile(r"\[Page\s+\d+\]\s*$")


def _extract_plain_text_pdf(pdf: Path) -> list[dict]:
    import fitz  # PyMuPDF
    doc = fitz.open(str(pdf))
    blocks: list[dict] = []
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_no = page_idx + 1
        page_height = page.rect.height
        counter = -1
        for x0, y0, x1, y1, text, _bn, btype in page.get_text("blocks"):
            if btype != 0:  # 0 = text, 1 = image
                continue
            text = text.rstrip()
            if not text.strip():
                continue

            # Chrome: single-line block within 60pt of top OR bottom is
            # almost always the RFC running header or "[Page N]" footer.
            # Also match the "[Page N]" pattern anywhere as a safety net.
            is_single_line = "\n" not in text
            near_top = y1 < 60
            near_bottom = y0 > page_height - 60
            if is_single_line and (near_top or near_bottom):
                continue
            if is_single_line and _RFC_PAGE_FOOTER.search(text):
                continue

            # Code/ASCII-art classification: a block with 2+ lines that
            # each carry pipe-delimited cells, or any line with a `+--`
            # separator, is a table/diagram. Preserve alignment.
            lines = text.split("\n")
            pipe_lines = sum(1 for ln in lines if ln.count("|") >= 2)
            has_sep = any("+--" in ln or "+-+" in ln for ln in lines)
            is_code = pipe_lines >= 2 or has_sep

            counter += 1
            bid = f"p{page_no}-b{counter}"
            if is_code:
                blocks.append({
                    "id": bid, "page": page_no, "section": "",
                    "type": "code", "text": text,
                })
            else:
                collapsed = " ".join(ln.strip() for ln in lines if ln.strip())
                blocks.append({
                    "id": bid, "page": page_no, "section": "",
                    "type": "text", "text": collapsed,
                })
    return blocks


_BACKENDS = {
    "docling": _extract_docling,
    "pymupdf4llm": _extract_pymupdf4llm,
    "pypdf": _extract_pypdf,
    "plain-text-pdf": _extract_plain_text_pdf,
}


# ─── Picture extraction (raster-image PDFs, pymupdf4llm-extracted) ───
#
# For PDFs without Parse's structural markup (AMD/Xilinx PG/UG docs and
# similar), Phase H picture extraction uses PyMuPDF directly:
#   - page.get_images() lists embedded raster image xrefs per page
#   - page.get_image_rects(xref) gives bboxes in PDF coords (points)
#   - text below the image (via get_text("blocks")) is scanned for
#     "Figure N-M" / "Figure N.M" caption patterns
#
# Caption-detection heuristic: look at text blocks within 10% page
# height below the image bbox, with x-overlap > 30%; if the block
# starts with "Figure <num>" treat it as the caption. Multiple matches
# concatenated.

_PICTURE_MIN_PNG_BYTES = 4000   # skip icons / decorations under ~100x100
_PICTURE_CAPTION_RE = re.compile(
    r"^\s*(Figure|Fig\.?|Table)\s+\d+", re.IGNORECASE
)
# Per-axis render-time padding around get_image_rects bbox. PyMuPDF's
# image-rect hugs the raster image tightly (no breathing room) on both
# axes, so we pad both directions. Slightly less on Y because adjacent
# paragraph text can sit very close above/below the figure. Stored bbox
# is NOT modified — render-only correction.
_BBOX_PAD_X_FRAC = float(os.environ.get("BBOX_PAD_X_FRAC", "0.02"))
_BBOX_PAD_Y_FRAC = float(os.environ.get("BBOX_PAD_Y_FRAC", "0.01"))


def _extract_pymupdf_pictures(pdf: Path, images_dir: Path) -> list[dict]:
    """For a non-Parse-extracted PDF, walk pages and emit type:picture
    blocks for every substantive raster image. Returns the picture
    blocks; caller merges them with text blocks from the main backend.

    Each picture block: bbox (normalised 0-1), caption (figure title
    text matched below the image, if any), image_path (relative to
    the collection folder), text (= caption initially for searchability).
    Persists the rendered PNG to images_dir/<pdf-stem>/<block-id>.png
    so caption-images.py can iterate without re-rendering.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf))
    pdf_img_dir = images_dir / pdf.stem
    blocks: list[dict] = []
    seen_xrefs: set[int] = set()

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_no = page_idx + 1
        page_w, page_h = page.rect.width, page.rect.height
        if page_w <= 0 or page_h <= 0:
            continue
        # Pre-fetch text blocks once per page for caption-pairing.
        text_blocks = page.get_text("blocks")

        pic_idx = 0
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            # Get position(s) where this image is placed on the page.
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            if not rects:
                continue
            rect = rects[0]  # first placement; an image reused on a
                             # different page is handled by xref-dedup
            if rect.width < 100 or rect.height < 100:
                continue
            # Render the bbox region (so the persisted PNG matches what
            # appears in the rendered page, including any overlays).
            # Pad the clip rect so edge annotations are recovered; stored
            # bbox below stays unpadded.
            try:
                zoom = 2048 / max(page_w, page_h)
                longer = max(page_w, page_h)
                pad_x_pt = _BBOX_PAD_X_FRAC * longer
                pad_y_pt = _BBOX_PAD_Y_FRAC * longer
                clip_rect = fitz.Rect(
                    max(0.0, rect.x0 - pad_x_pt),
                    max(0.0, rect.y0 - pad_y_pt),
                    min(page_w, rect.x1 + pad_x_pt),
                    min(page_h, rect.y1 + pad_y_pt),
                )
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                      clip=clip_rect, alpha=False)
                png_bytes = pix.tobytes("png")
            except Exception:
                continue
            if len(png_bytes) < _PICTURE_MIN_PNG_BYTES:
                continue

            # Caption pairing: find the CLOSEST text block below the
            # image that starts with "Figure N..." / "Table N...".
            # Only the closest match is the title — subsequent "Figure
            # 3-4 shows..." sentences are body text referring to the
            # NEXT figure, not the caption of THIS one.
            caption_candidates: list[tuple[float, str]] = []
            for tb in text_blocks:
                tx0, ty0, tx1, ty1, ttext, _bn, tt = tb
                if tt != 0:
                    continue
                if ty0 < rect.y1:
                    continue
                gap_pt = ty0 - rect.y1
                if gap_pt > 0.10 * page_h:
                    continue
                overlap = min(rect.x1, tx1) - max(rect.x0, tx0)
                tw = tx1 - tx0
                if tw <= 0 or overlap / tw < 0.30:
                    continue
                stripped = (ttext or "").strip()
                if not stripped:
                    continue
                if _PICTURE_CAPTION_RE.match(stripped):
                    caption_candidates.append((ty0, stripped))
            caption_candidates.sort()
            # Take only the closest caption. Some AMD-style captions
            # are multi-line in the same text block (e.g. "Figure 3-3:\n
            # Input - Rising Edge Sensitive..."), already concatenated
            # because get_text("blocks") returns the whole block as one
            # text string. So one match is enough.
            caption_txt = caption_candidates[0][1] if caption_candidates else ""

            pic_idx += 1
            pdf_img_dir.mkdir(parents=True, exist_ok=True)
            img_file = pdf_img_dir / f"p{page_no}-pic{pic_idx}.png"
            img_file.write_bytes(png_bytes)
            rel_path = img_file.relative_to(images_dir.parent).as_posix()

            # Bbox normalised to 0-1 page-relative coords (matching
            # the Parse-path schema).
            bbox = [rect.x0 / page_w, rect.y0 / page_h,
                    rect.x1 / page_w, rect.y1 / page_h]
            blocks.append({
                "id": f"p{page_no}-pic{pic_idx}",
                "page": page_no,
                "section": "",
                "type": "picture",
                "bbox": bbox,
                "caption": caption_txt,
                "image_path": rel_path,
                "text": caption_txt,
                "vlm_description": "",
            })

    return blocks


# ─── Driver ───────────────────────────────────────────────────────────────────

# Optional size cap (MB). PDFs larger than this are skipped with a warning so
# one huge document (e.g. the 94MB IEEE 802.3 standard) doesn't dominate the
# run or OOM the extractor. 0 = no cap.
_MAX_MB = float(os.environ.get("EXTRACT_MAX_MB", "0") or 0)


def process_collection(data_dir: Path, collection: str, backend: str, force: bool):
    folder = data_dir / collection
    cache = folder / ".rag-cache"
    cache.mkdir(exist_ok=True)
    # Per-collection persistent picture cache. extract.py creates it
    # for the non-Parse backends (Parse has its own picture-extraction
    # path inside extract-nemo.py with the same dir layout).
    images_dir = folder / ".rag-images"
    images_dir.mkdir(exist_ok=True)

    # Smallest first: the bulk of a collection becomes usable quickly and a
    # single giant PDF lands last instead of blocking everything behind it.
    pdfs = sorted(
        (p for p in folder.iterdir()
         if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: p.stat().st_size,
    )
    if not pdfs:
        print(f"[{collection}] no PDFs")
        return

    for pdf in pdfs:
        out = cache / (pdf.name + ".json")
        mtime = _iso_mtime(pdf)
        size_mb = pdf.stat().st_size / 1_000_000

        if not force and out.exists():
            try:
                if json.loads(out.read_text("utf-8")).get("source_mtime") == mtime:
                    print(f"[{collection}] skip (unchanged): {pdf.name}", flush=True)
                    continue
            except Exception:
                pass

        if _MAX_MB and size_mb > _MAX_MB:
            print(f"[{collection}] SKIP (>{_MAX_MB}MB, {size_mb:.0f}MB): "
                  f"{pdf.name} — set EXTRACT_MAX_MB=0 to include",
                  file=sys.stderr, flush=True)
            continue

        # Per-file backend override: .txt.pdf files are IETF RFCs and
        # similar text-source PDFs. The general-purpose backends mangle
        # their ASCII-art packet diagrams; route them to the dedicated
        # plain-text extractor instead.
        chosen_backend = (
            "plain-text-pdf" if pdf.name.lower().endswith(".txt.pdf") else backend
        )

        print(f"[{collection}] extracting ({chosen_backend}, {size_mb:.1f}MB): "
              f"{pdf.name} ...", flush=True)
        try:
            blocks = _BACKENDS[chosen_backend](pdf)
            # Picture extraction (Phase H pictures for non-Parse backends).
            # Parse already extracts pictures inside extract-nemo.py;
            # everything else needs this PyMuPDF-based pass to find
            # embedded raster images and emit type:picture blocks.
            if chosen_backend != "plain-text-pdf":
                # plain-text-pdf is for RFCs etc. — by convention they
                # have no figures worth captioning, skip the GPU later.
                try:
                    pic_blocks = _extract_pymupdf_pictures(pdf, images_dir)
                    if pic_blocks:
                        blocks = pic_blocks + blocks
                        print(f"[{collection}]   + {len(pic_blocks)} picture block(s)",
                              flush=True)
                except Exception as pic_exc:
                    print(f"[{collection}]   picture extraction failed: {pic_exc}",
                          file=sys.stderr, flush=True)
            blocks = _apply_toc(pdf, blocks)
        except Exception as exc:  # never let one bad PDF abort the run
            print(f"[{collection}] ERROR on {pdf.name}: {exc}", file=sys.stderr, flush=True)
            continue

        out.write_text(json.dumps({
            "doc": pdf.name,
            "source_mtime": mtime,
            "backend": chosen_backend,
            "blocks": blocks,
        }, ensure_ascii=False), "utf-8")
        print(f"[{collection}]   -> {len(blocks)} blocks -> {out.name}", flush=True)


def main(argv: list[str]):
    args = [a for a in argv if not a.startswith("--")]
    force = "--force" in argv

    if not args:
        sys.exit(__doc__)

    data_dir = Path(args[0]).resolve()
    if not data_dir.is_dir():
        sys.exit(f"data dir not found: {data_dir}")

    backend = _pick_backend()
    print(f"Extraction backend: {backend}")

    if len(args) > 1:
        collections = [args[1]]
    else:
        collections = sorted(
            e.name for e in data_dir.iterdir()
            if e.is_dir() and not e.name.startswith(".")
        )

    for c in collections:
        process_collection(data_dir, c, backend, force)

    print("Extraction complete.")


if __name__ == "__main__":
    main(sys.argv[1:])
