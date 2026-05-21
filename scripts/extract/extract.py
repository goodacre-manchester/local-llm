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


_BACKENDS = {
    "docling": _extract_docling,
    "pymupdf4llm": _extract_pymupdf4llm,
    "pypdf": _extract_pypdf,
}


# ─── Driver ───────────────────────────────────────────────────────────────────

# Optional size cap (MB). PDFs larger than this are skipped with a warning so
# one huge document (e.g. the 94MB IEEE 802.3 standard) doesn't dominate the
# run or OOM the extractor. 0 = no cap.
_MAX_MB = float(os.environ.get("EXTRACT_MAX_MB", "0") or 0)


def process_collection(data_dir: Path, collection: str, backend: str, force: bool):
    folder = data_dir / collection
    cache = folder / ".rag-cache"
    cache.mkdir(exist_ok=True)

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

        print(f"[{collection}] extracting ({backend}, {size_mb:.1f}MB): "
              f"{pdf.name} ...", flush=True)
        try:
            blocks = _BACKENDS[backend](pdf)
            blocks = _apply_toc(pdf, blocks)
        except Exception as exc:  # never let one bad PDF abort the run
            print(f"[{collection}] ERROR on {pdf.name}: {exc}", file=sys.stderr, flush=True)
            continue

        out.write_text(json.dumps({
            "doc": pdf.name,
            "source_mtime": mtime,
            "backend": backend,
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
