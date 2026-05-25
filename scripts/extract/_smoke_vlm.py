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
    python _smoke_vlm.py <pdf-path> [max=3] [model=qwen3-vl:8b] [prompt=v2] [start-page=1]

`prompt` selects which prompt to send:
  - v2   (default, locked-in): the propositional-only prompt iterated 2026-05-25.
  - cot  (experimental): Chain-of-Thought variant — three internal steps
         (transcribe labels / identify components / identify relationships)
         then propositional output only. User-suggested 2026-05-25 to test
         whether a forced internal transcription step improves grounding.

Example:
    python _smoke_vlm.py /mnt/d/Projects/local-llm/data/amd/pg099-axi-intc.pdf 3
    python _smoke_vlm.py /mnt/d/Projects/local-llm/data/amd/pg099-axi-intc.pdf 3 qwen3-vl:8b cot
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

from clean_vlm_caption import strip as clean_caption

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
PROMPT_V2 = (
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

# Experimental Chain-of-Thought variant (2026-05-25). Hypothesis: forcing
# an internal text-transcription step before generation grounds the output
# in actual labels rather than visual impressions, reducing hallucinated
# labels. Risk: CoT outputs tend verbose, so the prompt explicitly frames
# the reasoning as silent/internal and constrains output to propositions.
PROMPT_COT = (
    "This is a technical diagram from a specification or reference document.\n\n"
    "Analyse it in three internal steps before writing any output:\n"
    "  Step 1 (internal): Transcribe every text label visible in the diagram.\n"
    "  Step 2 (internal): Identify each labelled component.\n"
    "  Step 3 (internal): Identify the relationships, scopes, and "
    "constraints the diagram is asserting between those components.\n\n"
    "Then write the output. The output is a list of factual statements "
    "about what the diagram is asserting — the facts, relationships, "
    "scopes, and constraints it is communicating.\n\n"
    "Do not describe how the diagram is drawn, its visual structure, "
    "arrows, layout, spatial arrangement, or visual annotations (reference "
    "lines, dashed lines, grid lines, axes, color coding). Do not augment "
    "with background knowledge. If a component is labelled but its role is "
    "not explicitly stated, name it without interpreting it.\n\n"
    "Do not reference Step 1, Step 2, Step 3, or the analysis process. "
    "No introduction, no conclusion, no meta-commentary about \"the "
    "diagram\" itself. Begin with the first factual statement; end with "
    "the last."
)

# Experimental context-aware variant (2026-05-25). User-suggested: feed
# the surrounding document text alongside the image so the VLM has real
# anchors for component names, acronyms, and the diagram's scope —
# fighting the "fall back on training data" failure mode by replacing
# the vacuum with actual reference material. New failure-mode risk: the
# model might just echo the reference text instead of describing the
# image; the prompt explicitly forbids this and asks the model to only
# add facts that go beyond the reference.
#
# Contains {context} placeholder — formatted with page text at call time.
PROMPT_V2_CTX = (
    "Respond in English. Each line of your output is one complete "
    "sentence stating one fact the diagram communicates: typically a "
    "relationship between two named components (X provides Y to Z, X "
    "contains Y, X is connected to Y), a constraint on a component, "
    "or a property a component has.\n\n"
    "The image is a technical diagram from a specification document. The "
    "reference text below is from the same section — use it to spell "
    "component names and acronyms exactly as they appear there. Do not "
    "invent alternative spellings or expansions. Do not repeat facts that "
    "are already explicitly stated in the reference text. Do not analyse "
    "consistency between the diagram and the reference.\n\n"
    "<reference>\n{context}\n</reference>\n\n"
    "Do not describe how the diagram is drawn — no arrows, no layout, no "
    "spatial arrangement, no visual annotations. Do not augment with "
    "background knowledge beyond the reference text. If a component is "
    "labelled but its role is not in the reference text, state that the "
    "component exists without interpreting it.\n\n"
    "Output format: plain-text sentences, one per line. No numbered lists, "
    "no bulleted lists, no markdown headers, no bold, no section grouping, "
    "no introduction, no conclusion, no meta-commentary."
)

PROMPTS = {"v2": PROMPT_V2, "cot": PROMPT_COT, "v2-ctx": PROMPT_V2_CTX}

# Soft cap on context-text length when injecting into v2-ctx prompt.
# IEEE-spec pages typically have 1500-3000 chars; we cap generously to
# absorb dense pages but truncate runaway page-content overflow.
_CONTEXT_MAX_CHARS = 4000


def _page_context(doc, page_no_0indexed: int) -> str:
    """Pull text from the image's page plus the page before and after,
    joined with a separator. Captures captions that wrap across pages
    and surrounding subsection text that anchors what the diagram is.
    Truncates to _CONTEXT_MAX_CHARS to keep prompt size bounded."""
    parts: list[str] = []
    for offset in (-1, 0, 1):
        idx = page_no_0indexed + offset
        if 0 <= idx < len(doc):
            text = doc[idx].get_text().strip()
            if text:
                parts.append(text)
    joined = "\n\n".join(parts)
    if len(joined) > _CONTEXT_MAX_CHARS:
        joined = joined[:_CONTEXT_MAX_CHARS] + "\n\n[…context truncated…]"
    return joined


def _iter_embedded_images(pdf_path: Path, max_images: int, start_page: int = 1):
    """Walk the PDF from `start_page` (1-indexed, inclusive), yield
    (page_no, image_index, png_bytes, context_text) for the first
    `max_images` embedded raster images. Uses PyMuPDF's get_images()
    which pulls embedded raster bytes directly — no rendering needed."""
    doc = fitz.open(str(pdf_path))
    yielded = 0
    seen_xrefs: set[int] = set()
    for page_no in range(max(0, start_page - 1), len(doc)):
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
            yield (page_no + 1, xref, png, _page_context(doc, page_no))
            yielded += 1
            if yielded >= max_images:
                return


def _caption(model: str, png_bytes: bytes, prompt: str) -> tuple[str, float]:
    """Send one image to the VLM with the chosen prompt. Returns
    (caption_text, elapsed_seconds)."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    body = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt,
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
    prompt_key = argv[3] if len(argv) > 3 else "v2"
    start_page = int(argv[4]) if len(argv) > 4 else 1
    if prompt_key not in PROMPTS:
        sys.exit(f"Unknown prompt '{prompt_key}'; choose one of: "
                 f"{', '.join(PROMPTS)}")
    prompt = PROMPTS[prompt_key]

    print(f"[smoke-vlm] pdf       = {pdf.name}", flush=True)
    print(f"[smoke-vlm] max       = {max_images}", flush=True)
    print(f"[smoke-vlm] model     = {model}", flush=True)
    print(f"[smoke-vlm] prompt-id = {prompt_key}", flush=True)
    print(f"[smoke-vlm] start     = p.{start_page}", flush=True)
    print(f"[smoke-vlm] prompt    = {prompt[:80]}...", flush=True)
    print(flush=True)

    n = 0
    uses_context = "{context}" in prompt
    for page_no, xref, png, ctx in _iter_embedded_images(pdf, max_images, start_page):
        n += 1
        ctx_note = f", ctx {len(ctx)} chars" if uses_context else ""
        print(f"=== Image {n} — p.{page_no} (xref {xref}, "
              f"{len(png)/1024:.1f} KB{ctx_note}) ===", flush=True)
        if uses_context:
            filled_prompt = prompt.format(context=ctx)
        else:
            filled_prompt = prompt
        try:
            caption, dt = _caption(model, png, filled_prompt)
        except Exception as exc:
            print(f"  ERROR: {exc}", flush=True)
            continue
        cleaned = clean_caption(caption)
        print(f"  [{dt:.1f}s] caption (RAW, {len(caption)} chars):")
        for line in caption.splitlines():
            print(f"    {line}")
        delta = len(caption) - len(cleaned)
        print(f"  -- caption (STRIPPED, {len(cleaned)} chars, -{delta}):")
        for line in cleaned.splitlines():
            print(f"    {line}")
        print(flush=True)

    if n == 0:
        print("(no embedded raster images found in this PDF — try one with figures)",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
