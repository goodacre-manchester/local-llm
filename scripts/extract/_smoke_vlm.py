#!/usr/bin/env python3
"""
VLM captioning smoke test for Phase F (image captioning sweep).

Pulls embedded raster images from PDFs in data/<collection>/, calls a
local Ollama-hosted vision model with the canonical Phase F prompt,
prints the resulting caption to stdout. Used to gut-check (a) the
prompt produces RAG-friendly propositional captions vs visual
descriptions and (b) the chosen VLM follows the suppression
instructions in the prompt.

Pure stdlib + PyMuPDF + urllib. Runs in the existing scripts/extract
.venv (no need to load the heavy .venv-nemo). Talks to Ollama at the
host's localhost:11434.

Usage (inside the lightweight scripts/extract/.venv):
    python _smoke_vlm.py <pdf-path> [max=3] [model=qwen3-vl:8b]

Example:
    python _smoke_vlm.py /mnt/d/Projects/local-llm/data/amd/pg099-axi-intc.pdf 3
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import fitz  # PyMuPDF

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

# Phase F captioning prompt — user-developed; iterated 2026-05-25.
#
# Iteration history (kept for context — see also Phase F section of
# NEXT-STEPS-STACK-ADOPTION.md):
#   v1 (original): worked but visual references slipped through ("the red
#       dashed vertical lines") and ~30% of outputs had meta-commentary
#       openings ("The diagram asserts the following...").
#   v2: added explicit suppression of visual annotations (reference lines,
#       dashed lines, grid lines, axes, color coding), anti-vague-filler,
#       and explicit "no introduction, no conclusion" instructions.
#       Best result so far on qwen3-vl:8b — fixed color-reference leak,
#       improved most images, but meta-commentary still leaked on some
#       complex diagrams.
#   v3 (DO/DON'T few-shot examples): BACKFIRED. The model parroted the
#       DON'T examples back as DO content ("The diagram shows..." appeared
#       in the OUTPUT despite being listed in DO NOT). This is a known LLM
#       failure mode — negation in instructions is unreliable; few-shot
#       negative examples can act as targets rather than anti-targets.
#       REVERTED to v2.
#
# Current locked-in prompt below = iteration v2.
PROMPT = (
    "This is a technical diagram from a specification or reference document. "
    "State only what the diagram is asserting — the facts, relationships, "
    "scopes, and constraints it is communicating.\n\n"
    "Do not describe how the diagram is drawn, its visual structure, arrows, "
    "layout, spatial arrangement, or visual annotations (reference lines, "
    "dashed lines, grid lines, axes, color coding). Do not augment with "
    "background knowledge. If a component is labelled but its role is not "
    "explicitly stated, name it without interpreting it. If the diagram does "
    "not specify a precise value, state what it does specify rather than "
    "using vague placeholders.\n\n"
    "Output ONLY the factual statements the diagram makes. No introduction, "
    "no conclusion, no meta-commentary about \"the diagram\" itself. Begin "
    "with the first fact; end with the last."
)


def _iter_embedded_images(pdf_path: Path, max_images: int):
    """Walk the PDF, yield (page_no, image_index, png_bytes) for the first
    `max_images` embedded raster images. Uses PyMuPDF's get_images() which
    pulls embedded raster bytes directly — no rendering needed."""
    doc = fitz.open(str(pdf_path))
    yielded = 0
    seen_xrefs: set[int] = set()
    for page_no in range(len(doc)):
        page = doc[page_no]
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha or pix.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                png = pix.tobytes("png")
            except Exception as exc:
                print(f"  page {page_no+1}: failed to extract xref {xref}: {exc}",
                      file=sys.stderr)
                continue
            # Skip very small images (icons, decorations) — typically <100x100
            if len(png) < 4000:
                continue
            yield (page_no + 1, xref, png)
            yielded += 1
            if yielded >= max_images:
                return


def _caption(model: str, png_bytes: bytes) -> tuple[str, float]:
    """Send one image to the VLM with the canonical prompt. Returns
    (caption_text, elapsed_seconds)."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    body = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": PROMPT,
                "images": [b64],
            }
        ],
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    dt = time.time() - t0
    text = (payload.get("message", {}) or {}).get("content", "") or ""
    return text.strip(), dt


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit(__doc__)
    pdf = Path(argv[0]).resolve()
    if not pdf.is_file():
        sys.exit(f"PDF not found: {pdf}")
    max_images = int(argv[1]) if len(argv) > 1 else 3
    model = argv[2] if len(argv) > 2 else "qwen3-vl:8b"

    print(f"[smoke-vlm] pdf       = {pdf.name}", flush=True)
    print(f"[smoke-vlm] max       = {max_images}", flush=True)
    print(f"[smoke-vlm] model     = {model}", flush=True)
    print(f"[smoke-vlm] prompt    = {PROMPT[:80]}...", flush=True)
    print(flush=True)

    n = 0
    for page_no, xref, png in _iter_embedded_images(pdf, max_images):
        n += 1
        print(f"=== Image {n} — p.{page_no} (xref {xref}, "
              f"{len(png)/1024:.1f} KB) ===", flush=True)
        try:
            caption, dt = _caption(model, png)
        except Exception as exc:
            print(f"  ERROR: {exc}", flush=True)
            continue
        print(f"  [{dt:.1f}s] caption:")
        for line in caption.splitlines():
            print(f"    {line}")
        print(flush=True)

    if n == 0:
        print("(no embedded raster images found in this PDF — try one with figures)",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
