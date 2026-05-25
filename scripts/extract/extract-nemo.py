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


# Strip Parse's bbox / class metadata so downstream md parser sees clean
# headings / tables / paragraphs. Patterns observed in actual output:
#   <x_0.0859><y_0.0547> ...
#   <class_Picture>, <class_List-item>, <class_Page-header>, etc.
_COORD_RE = re.compile(r"<x_[\d.]+>\s*<y_[\d.]+>\s*", flags=re.DOTALL)
_CLASS_RE = re.compile(r"<class_[A-Za-z0-9_-]+>", flags=re.DOTALL)
_OLD_BOX_RE = re.compile(r"<box>.*?</box>", flags=re.DOTALL)  # alt form some versions used
_OLD_CLS_RE = re.compile(r"<cls>.*?</cls>", flags=re.DOTALL)


def _clean_parse_md(raw: str) -> str:
    cleaned = _COORD_RE.sub("", raw)
    cleaned = _CLASS_RE.sub("", cleaned)
    cleaned = _OLD_BOX_RE.sub("", cleaned)
    cleaned = _OLD_CLS_RE.sub("", cleaned)
    return cleaned


def _extract_nemotron_parse(pdf: Path) -> list[dict]:
    section_state = {"current": ""}
    blocks: list[dict] = []
    doc = fitz.open(str(pdf))
    n_pages = len(doc)

    if PAGE_FILTER is not None:
        print(f"    NEMO_PARSE_PAGES filter active: "
              f"{len(PAGE_FILTER)} of {n_pages} pages will be processed", flush=True)

    page_errors = 0
    for i in range(n_pages):
        page_no = i + 1
        if PAGE_FILTER is not None and page_no not in PAGE_FILTER:
            continue
        try:
            png = _render_page_png(doc[i])
            if not png:
                continue
            raw = _parse_image(png)
            cleaned = _clean_parse_md(raw)
        except Exception as exc:
            page_errors += 1
            print(f"    page {page_no}: ERROR {exc}", flush=True)
            continue

        blocks.extend(_md_page_to_blocks(cleaned, page_no, section_state))

        if page_no % 25 == 0 or page_no == n_pages:
            print(f"    page {page_no}/{n_pages}: {len(blocks)} blocks so far"
                  + (f" ({page_errors} errors)" if page_errors else ""),
                  flush=True)

    if page_errors:
        print(f"    WARNING: {page_errors}/{n_pages} pages had extraction errors", flush=True)
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
                blocks = _extract_nemotron_parse(pdf)
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
