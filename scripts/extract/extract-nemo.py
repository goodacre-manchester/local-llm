#!/usr/bin/env python3
"""
Nemotron Parse v1.2 extractor -- in-process HF transformers version.

This is NVIDIA's documented "Option B" path from the Parse model card:
load Parse via `AutoModel.from_pretrained(..., trust_remote_code=True)`,
process pages with the bundled processor, generate with the model's bundled
GenerationConfig (which has the right beam-search / sampling defaults for
stable output).

The vLLM-served alternative (Option A) was implemented first but found to
produce degenerate / token-collapse output reliably -- the bundled
GenerationConfig isn't applied through vLLM's chat completions API. The
vLLM compose service entry is kept around in docker-compose.yml as
nemo-parse, currently stopped, in case a future vLLM release fixes this.

Sidecar shape is identical to extract.py output (backend="nemotron-parse-v1.2")
so app/server.js's loadSidecar/chunkBlocks consume both interchangeably.
Shared helpers (markdown -> blocks, TOC overlay, mtime) reused from extract.py.

Usage (matches extract.py):
  python extract-nemo.py <data_dir> [collection] [--force]

Env overrides:
  NEMO_PARSE_MODEL_ID     nvidia/NVIDIA-Nemotron-Parse-v1.2
  NEMO_PARSE_TARGET_PX    2048    (render so longer page side = this many px)
  NEMO_PARSE_DEVICE       cuda:0  (or cpu for offload/test)
  NEMO_PARSE_PAGES        ""      (e.g. "203-238,480-482" - 1-indexed page allowlist
                                   for partial extraction; pages keep their
                                   ORIGINAL numbers so _apply_toc still resolves
                                   sections correctly from the PDF bookmarks)
  HF_HOME                 ~/.cache/huggingface (set to share with vLLM cache)
"""

from __future__ import annotations

import gc
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF
import torch
from PIL import Image
from transformers import AutoModel, AutoProcessor, GenerationConfig

# Reuse the established helpers from extract.py (markdown -> blocks, TOC overlay,
# mtime serialisation). Same directory -> direct import.
from extract import _md_page_to_blocks, _apply_toc, _iso_mtime
from sanitize_collapse import sanitize_sidecar

MODEL_ID  = os.environ.get("NEMO_PARSE_MODEL_ID", "nvidia/NVIDIA-Nemotron-Parse-v1.2")
TARGET_PX = int(os.environ.get("NEMO_PARSE_TARGET_PX", "2048"))
DEVICE    = os.environ.get("NEMO_PARSE_DEVICE",    "cuda:0")
PAGES_ENV = os.environ.get("NEMO_PARSE_PAGES",     "").strip()
PROMPT    = "</s><s><predict_bbox><predict_classes><output_markdown><predict_no_text_in_pic>"


def _parse_pages_env(spec: str) -> set[int] | None:
    """"203-238,480-482" -> {203, 204, ..., 238, 480, 481, 482}. Empty -> None
    (meaning: all pages)."""
    if not spec:
        return None
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, hi = chunk.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(chunk))
    return out


PAGE_FILTER = _parse_pages_env(PAGES_ENV)

# Lazy globals so import doesn't trigger model load (helps the --help/error path).
_model: AutoModel | None = None
_processor: AutoProcessor | None = None
_gen_config: GenerationConfig | None = None


def _load_model():
    global _model, _processor, _gen_config
    if _model is not None:
        return
    print(f"[nemo-parse] loading {MODEL_ID} on {DEVICE} (first call) ...", flush=True)
    t = time.time()
    _processor  = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
    _gen_config = GenerationConfig.from_pretrained(MODEL_ID, trust_remote_code=True)
    _model = AutoModel.from_pretrained(
        MODEL_ID,
        trust_remote_code=True,
        torch_dtype=torch.bfloat16,
    ).to(DEVICE).eval()
    print(f"[nemo-parse] model loaded in {time.time() - t:.1f}s", flush=True)


def _render_page_png(page: fitz.Page) -> bytes:
    """Render a PDF page to PNG, longer side ~= TARGET_PX."""
    r = page.rect
    longer_pt = max(r.width, r.height)
    if longer_pt <= 0:
        return b""
    zoom = TARGET_PX / longer_pt
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def _parse_image(png_bytes: bytes) -> str:
    """Run one image through Parse and return the raw text output (still
    contains <x_..><y_..> coord tokens and <class_..> tokens -- caller cleans)."""
    _load_model()
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    inputs = _processor(
        images=[image],
        text=PROMPT,
        return_tensors="pt",
        add_special_tokens=False,
    ).to(DEVICE)
    with torch.no_grad():
        outputs = _model.generate(**inputs, generation_config=_gen_config)
    text = _processor.batch_decode(outputs, skip_special_tokens=True)[0]
    return text


# Strip Parse's bbox / class / structural metadata so downstream md parser
# sees clean headings / tables / paragraphs. Patterns observed in actual
# Parse v1.2 output (and confirmed via 2026-05-25 IEEE corpus scan):
#   <x_0.0859><y_0.0547> ...   coord markers (paired)
#   <y_0.725>                  orphan coord (when <x_> was clipped)
#   <class_Picture>, <class_List-item>, <class_Page-header>, etc.
#   <tbc>                      "table being continued" — Parse emits at
#                              end of every TOC entry / table row Parse
#                              thinks spans pages; 1205 across IEEE corpus
#   <u>...</u>                 HTML underline — Parse wraps URLs; no
#                              markdown equivalent + noise for embeddings;
#                              strip the tags, keep inner text
#   <box>...</box> / <cls>...</cls>   older Parse versions
#
# IMPORTANT: many IEEE specs use angle-bracket notation in content
# (e.g. `\<ESP-DA, ESP-SA, ESP-VID\>` for a 3-tuple, `\<MaxFrameSize\>`
# for a placeholder). These appear with backslash-escaping in Parse
# output and MUST be preserved. None of the strip patterns below match
# escaped forms — they only match Parse-emitted unescaped markers.
_COORD_RE      = re.compile(r"<x_[\d.]+>\s*<y_[\d.]+>\s*", flags=re.DOTALL)
_ORPHAN_COORD  = re.compile(r"<[xy]_[\d.]+>")
_CLASS_RE      = re.compile(r"<class_[A-Za-z0-9_-]+>", flags=re.DOTALL)
_TBC_RE        = re.compile(r"<tbc>")
_U_TAG_RE      = re.compile(r"</?u>")
_OLD_BOX_RE    = re.compile(r"<box>.*?</box>", flags=re.DOTALL)
_OLD_CLS_RE    = re.compile(r"<cls>.*?</cls>", flags=re.DOTALL)


def _clean_parse_md(raw: str) -> str:
    cleaned = _COORD_RE.sub("", raw)
    cleaned = _ORPHAN_COORD.sub("", cleaned)
    cleaned = _CLASS_RE.sub("", cleaned)
    cleaned = _TBC_RE.sub("", cleaned)
    cleaned = _U_TAG_RE.sub("", cleaned)
    cleaned = _OLD_BOX_RE.sub("", cleaned)
    cleaned = _OLD_CLS_RE.sub("", cleaned)
    return cleaned


# ─── Picture-block extraction (Phase H + F prerequisite) ──────────────
#
# Parse v1.2 tags every layout block as
#     <x_x1><y_y1>CONTENT<x_x2><y_y2><class_CLASS>
# where (x1,y1)-(x2,y2) is the normalised bbox and CLASS is one of
# Text, Section-header, Caption, Picture, Page-header, Page-footer,
# Footnote, List-item. The existing _clean_parse_md strips all of this
# so the downstream markdown parser sees clean prose. But for picture
# blocks we WANT the bbox preserved so we can:
#   1. Rasterise the picture region from the source PDF to a persistent
#      PNG (so a future re-captioning run can swap the VLM without
#      re-running Parse OR re-rasterising).
#   2. Pair each picture with its caption (the Caption-class block
#      directly below it) for use as Phase F prompt context.
#   3. Emit a type:"picture" block alongside text blocks so downstream
#      consumers (rag-server chunk ingest, dump-sidecar-md renderer)
#      can treat pictures as first-class content.

_BLOCK_RE = re.compile(
    r"<x_(?P<x1>[\d.]+)><y_(?P<y1>[\d.]+)>"
    r"(?P<content>.*?)"
    r"<x_(?P<x2>[\d.]+)><y_(?P<y2>[\d.]+)>"
    r"<class_(?P<cls>[A-Za-z0-9_-]+)>",
    flags=re.DOTALL,
)


def _parse_raw_blocks(raw: str) -> list[dict]:
    """Parse Parse's raw markdown into structured per-block records.
    Each record: {bbox, cls, content, span} where span is the (start,end)
    char offset of the match in raw — used later by _strip_consumed_spans
    to remove picture + paired-caption regions before text extraction so
    captions don't duplicate."""
    out: list[dict] = []
    for m in _BLOCK_RE.finditer(raw):
        out.append({
            "bbox": [float(m["x1"]), float(m["y1"]),
                     float(m["x2"]), float(m["y2"])],
            "cls": m["cls"],
            "content": m["content"],
            "span": (m.start(), m.end()),
        })
    return out


def _strip_consumed_spans(raw: str, consumed_match_keys: set[tuple[int, int]]) -> str:
    """Re-walk `raw` with _BLOCK_RE and replace any block whose match
    span is in `consumed_match_keys` with an empty string. Used to
    remove picture + caption regions from the raw markdown after they've
    been consumed as picture-block metadata, so the same caption text
    doesn't ALSO show up as a duplicate paragraph from _md_page_to_blocks."""
    pieces: list[str] = []
    last = 0
    for m in _BLOCK_RE.finditer(raw):
        if (m.start(), m.end()) in consumed_match_keys:
            pieces.append(raw[last:m.start()])
            last = m.end()
    pieces.append(raw[last:])
    return "".join(pieces)


def _pair_picture_captions(parsed: list[dict]) -> tuple[list[dict], set[tuple[int, int]]]:
    """For each Picture block, concatenate ALL Caption blocks within
    15% of page height below it (and with x-overlap > 50%). Multiple
    captions per picture are common — a Figure 6-2 may have:
      1. an abbreviation legend ("LSAP - Link service access point")
      2. the formal title ("**Figure 6-2--Relationship between ...**")
    Picking just the closest can pick the legend over the title; this
    concatenates them in document order (top-to-bottom) so both end up
    in the caption field and the VLM has the full label context.

    Returns (pictures_with_captions, consumed_spans) where consumed_spans
    is the set of (start, end) match offsets in raw that should be
    stripped from the markdown before text extraction — so consumed
    captions don't duplicate as standalone paragraphs.

    Y-gap threshold: empirical from the 2026-05-26 probe — well-formed
    figure-title captions sit ~1-3% below the picture bottom; with a
    legend stacked between, the second caption can be at +5-8%. 15% is
    generous enough to catch multi-row labels.
    """
    pictures = [b for b in parsed if b["cls"] == "Picture"]
    captions = [b for b in parsed if b["cls"] == "Caption"]
    consumed: set[tuple[int, int]] = set()
    for pic in pictures:
        consumed.add(pic["span"])
        pic_bottom = pic["bbox"][3]
        pic_left, pic_right = pic["bbox"][0], pic["bbox"][2]
        matched: list[tuple[float, dict]] = []
        for cap in captions:
            cap_top = cap["bbox"][1]
            if cap_top < pic_bottom:
                continue
            gap = cap_top - pic_bottom
            if gap > 0.15:
                continue
            cap_left, cap_right = cap["bbox"][0], cap["bbox"][2]
            overlap = min(pic_right, cap_right) - max(pic_left, cap_left)
            cap_width = cap_right - cap_left
            if cap_width <= 0 or overlap / cap_width < 0.5:
                continue
            matched.append((cap_top, cap))
        matched.sort()
        pic["caption"] = "\n\n".join(
            c["content"].strip() for _, c in matched if c["content"].strip()
        )
        for _, c in matched:
            consumed.add(c["span"])
    return pictures, consumed


def _rasterise_bbox(page: fitz.Page, bbox: list[float],
                    target_long_side: int = TARGET_PX) -> bytes:
    """Render the bbox-cropped region of `page` to PNG. bbox is in
    Parse's normalised 0-1 coordinates. Render zoom matches the full-
    page Parse render so figure detail (annotations, axis labels) stays
    readable for the VLM."""
    r = page.rect
    longer = max(r.width, r.height)
    if longer <= 0:
        return b""
    zoom = target_long_side / longer
    x1, y1, x2, y2 = bbox
    clip = fitz.Rect(x1 * r.width, y1 * r.height,
                     x2 * r.width, y2 * r.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          clip=clip, alpha=False)
    return pix.tobytes("png")


def _extract_nemotron_parse(pdf: Path, images_dir: Path | None = None) -> list[dict]:
    section_state = {"current": ""}
    blocks: list[dict] = []
    doc = fitz.open(str(pdf))
    n_pages = len(doc)

    if PAGE_FILTER is not None:
        print(f"    NEMO_PARSE_PAGES filter active: "
              f"{len(PAGE_FILTER)} of {n_pages} pages will be processed", flush=True)

    page_errors = 0
    total_pictures = 0
    for i in range(n_pages):
        page_no = i + 1
        if PAGE_FILTER is not None and page_no not in PAGE_FILTER:
            continue
        try:
            png = _render_page_png(doc[i])
            if not png:
                continue
            raw = _parse_image(png)
        except Exception as exc:
            page_errors += 1
            print(f"    page {page_no}: ERROR {exc}", flush=True)
            continue

        # Picture extraction: pulled from raw (pre-cleaning) markdown.
        # Emit BEFORE text blocks per page so the figure appears at the
        # top of its page in the rendered .md (text references usually
        # come below). Inside Chroma the relative ordering doesn't matter
        # — chunkBlocks groups by clauseKey + size, not position.
        #
        # When images_dir is provided, also remove the picture + paired-
        # caption regions from the raw markdown before text extraction
        # so the same caption text doesn't show up as a duplicate
        # paragraph in the .md preview AND a duplicate chunk in Chroma.
        consumed_spans: set[tuple[int, int]] = set()
        if images_dir is not None:
            try:
                parsed = _parse_raw_blocks(raw)
                pictures, consumed_spans = _pair_picture_captions(parsed)
                for pic_idx, pic in enumerate(pictures, 1):
                    try:
                        png_bytes = _rasterise_bbox(doc[i], pic["bbox"])
                    except Exception as raster_exc:
                        print(f"    page {page_no} pic{pic_idx}: rasterise "
                              f"failed: {raster_exc}", flush=True)
                        continue
                    pdf_img_dir = images_dir / pdf.stem
                    pdf_img_dir.mkdir(parents=True, exist_ok=True)
                    img_file = pdf_img_dir / f"p{page_no}-pic{pic_idx}.png"
                    img_file.write_bytes(png_bytes)
                    rel_path = img_file.relative_to(images_dir.parent).as_posix()
                    caption_txt = pic.get("caption", "")
                    blocks.append({
                        "id": f"p{page_no}-pic{pic_idx}",
                        "page": page_no,
                        "section": section_state.get("current", ""),
                        "type": "picture",
                        "bbox": pic["bbox"],
                        "caption": caption_txt,
                        "image_path": rel_path,
                        # `text` = caption initially so the block is
                        # searchable on the figure title from the moment
                        # Phase H lands. caption-images.py will append
                        # the VLM description later.
                        "text": caption_txt,
                        "vlm_description": "",
                    })
                    total_pictures += 1
            except Exception as exc:
                print(f"    page {page_no}: picture extraction failed: {exc}",
                      flush=True)

        # Strip consumed picture+caption spans from raw BEFORE cleaning
        # so they don't double up as text blocks.
        if consumed_spans:
            raw_for_text = _strip_consumed_spans(raw, consumed_spans)
        else:
            raw_for_text = raw
        cleaned = _clean_parse_md(raw_for_text)
        blocks.extend(_md_page_to_blocks(cleaned, page_no, section_state))

        if page_no % 25 == 0 or page_no == n_pages:
            print(f"    page {page_no}/{n_pages}: {len(blocks)} blocks so far"
                  + (f" ({page_errors} errors)" if page_errors else ""),
                  flush=True)

    if page_errors:
        print(f"    WARNING: {page_errors}/{n_pages} pages had extraction errors", flush=True)
    if images_dir is not None and total_pictures:
        print(f"    extracted {total_pictures} picture block(s) -> "
              f"{images_dir.name}/{pdf.stem}/", flush=True)
    return blocks


def main(argv: list[str]):
    args = [a for a in argv if not a.startswith("--")]
    force = "--force" in argv

    if not args:
        sys.exit(__doc__)

    data_dir = Path(args[0]).resolve()
    if not data_dir.is_dir():
        sys.exit(f"data dir not found: {data_dir}")

    if len(args) > 1:
        collections = [args[1]]
    else:
        collections = sorted(
            e.name for e in data_dir.iterdir()
            if e.is_dir() and not e.name.startswith(".")
        )

    # Fail fast if CUDA isn't there (Parse on CPU would be impractically slow).
    if DEVICE.startswith("cuda") and not torch.cuda.is_available():
        sys.exit("CUDA not available -- Parse on CPU is impractical. "
                 "Set NEMO_PARSE_DEVICE=cpu to override.")

    for collection in collections:
        folder = data_dir / collection
        cache = folder / ".rag-cache"
        cache.mkdir(exist_ok=True)
        # Per-collection persistent image cache. Pictures extracted from
        # PDFs land here so caption-images.py (and any future re-captioning
        # run with a different VLM) can iterate the persisted PNGs without
        # re-running Parse.
        images_dir = folder / ".rag-images"
        images_dir.mkdir(exist_ok=True)
        # Skip *.txt.pdf — IETF RFCs and similar text-source PDFs.
        # Parse mis-converts ASCII-art packet diagrams into broken LaTeX
        # tabular fragments (see rfc4541 §IPv6-multicast-address-format
        # for the canonical failure). These files have no visual content
        # that benefits from Parse; pymupdf4llm via extract.py handles
        # them natively. If a non-RFC *.txt.pdf legitimately needs Parse
        # in the future, add an opt-in flag — until then, the skip is
        # unambiguous and worth more than the flexibility.
        pdfs = sorted(
            (p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() == ".pdf"
             and not p.name.lower().endswith(".txt.pdf")),
            key=lambda p: p.stat().st_size,
        )
        skipped_txt = sorted(
            p.name for p in folder.iterdir()
            if p.is_file() and p.name.lower().endswith(".txt.pdf")
        )
        for s in skipped_txt:
            print(f"[{collection}] skip (.txt.pdf, use extract.py): {s}", flush=True)
        if not pdfs:
            print(f"[{collection}] no PDFs (after .txt.pdf filter)")
            continue

        for pdf in pdfs:
            out = cache / (pdf.name + ".json")
            mtime = _iso_mtime(pdf)
            size_mb = pdf.stat().st_size / 1_000_000

            if not force and out.exists():
                try:
                    cached = json.loads(out.read_text("utf-8"))
                    if cached.get("source_mtime") == mtime \
                       and cached.get("backend") == "nemotron-parse-v1.2":
                        print(f"[{collection}] skip (unchanged, nemotron-parse): {pdf.name}", flush=True)
                        continue
                except Exception:
                    pass

            print(f"[{collection}] extracting (nemotron-parse-v1.2, {size_mb:.1f}MB): "
                  f"{pdf.name}", flush=True)
            try:
                blocks = _extract_nemotron_parse(pdf, images_dir=images_dir)
                blocks = _apply_toc(pdf, blocks)
            except Exception as exc:
                print(f"[{collection}] ERROR on {pdf.name}: {exc}",
                      file=sys.stderr, flush=True)
                continue

            # Defensive: Parse occasionally enters a token-collapse
            # runaway (single short token regenerated 100+ times). The
            # 2026-05-25 IEEE sweep found 31 such blocks across 12 PDFs;
            # every one of them was actively poisoning Chroma. Sanitize
            # before write so new sidecars never carry the cascade.
            sidecar_dict = {
                "doc": pdf.name,
                "source_mtime": mtime,
                "backend": "nemotron-parse-v1.2",
                "blocks": blocks,
            }
            _, changes = sanitize_sidecar(sidecar_dict)
            if changes:
                total_saved = sum(c["before_len"] - c["after_len"]
                                   for c in changes)
                print(f"[{collection}]   sanitized {len(changes)} collapse "
                      f"block(s) ({total_saved} chars removed)", flush=True)
            out.write_text(json.dumps(sidecar_dict, ensure_ascii=False), "utf-8")
            print(f"[{collection}]   -> {len(blocks)} blocks -> {out.name}", flush=True)

    # Free GPU memory before exit so the next process (Ollama, sd-webui) can
    # claim VRAM without an explicit reboot.
    global _model
    if _model is not None:
        del _model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    print("Extraction complete.")


if __name__ == "__main__":
    main(sys.argv[1:])
