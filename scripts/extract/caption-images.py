#!/usr/bin/env python3
"""
Phase F: VLM-caption every picture block in a collection's Parse
sidecars. For each type:"picture" block:

  1. Read the persisted PNG at <collection>/<image_path>
  2. Build a context window from neighbouring text blocks (same
     section if available, otherwise ±2 pages)
  3. Call the locked-in qwen3-vl:8b VLM with the v2-ctx prompt
     (Ollama /api/chat) — image + reference context
  4. Strip framing leak via clean_vlm_caption.strip
  5. Write the stripped output into the block's `vlm_description`
     field AND append it to `text` so the chunk RAG-server emits
     includes both the figure title and the propositional description

Resumable: skips blocks that already have a non-empty vlm_description.
Pass `--force` to re-caption all blocks regardless.

Pure stdlib + clean_vlm_caption (which is also pure stdlib). Talks to
Ollama at http://127.0.0.1:11434/api/chat. No heavy deps.

Usage:
    python caption-images.py <data_dir>                 # all collections
    python caption-images.py <data_dir> <collection>    # one collection
    python caption-images.py <data_dir> ieee --force    # re-caption all
    python caption-images.py <data_dir> ieee --only 8021AB-2016.pdf

Tunables (env):
    OLLAMA_URL          default http://127.0.0.1:11434/api/chat
    VLM_MODEL           default qwen3-vl:8b
    CAPTION_CTX_CHARS   max reference-text length per call (default 4000)
    CAPTION_CTX_PAGES   ± page window for neighbour gathering (default 2)
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# clean_vlm_caption lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean_vlm_caption import strip as strip_caption  # noqa: E402


OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
VLM_MODEL   = os.environ.get("VLM_MODEL", "qwen3-vl:8b")
CTX_CHARS   = int(os.environ.get("CAPTION_CTX_CHARS", "4000"))
CTX_PAGES   = int(os.environ.get("CAPTION_CTX_PAGES", "2"))
# Persist the sidecar JSON to disk every N captioned pictures. Large
# files (e.g. 802-3-2022 with 2251 pictures @ ~60s/pic = ~33h) would
# otherwise buffer the entire run's work in memory and lose it all on
# any process death. 25 = a checkpoint every ~25 minutes — small enough
# that crash-recovery loses at most one batch, large enough that disk
# IO doesn't dominate.
CHECKPOINT_EVERY = int(os.environ.get("CAPTION_CHECKPOINT_EVERY", "25"))

# Locked-in v2-ctx prompt from the 2026-05-25 Phase F smoke. Lives here
# (not imported from _smoke_vlm.py) so the production captioner is
# decoupled from the dev harness — changes to the smoke harness's
# prompt iteration shouldn't silently change production captions.
PROMPT_V2_CTX_TEMPLATE = (
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


def _build_context(blocks: list[dict], pic_idx: int, max_chars: int) -> str:
    """Gather neighbouring text-block content for the v2-ctx prompt.

    Strategy:
      1. Same-section preference: include all text blocks within the
         CTX_PAGES window that share the picture's `section` field.
      2. Fallback to ±CTX_PAGES window regardless of section if step 1
         yields too little.
      3. Truncate to max_chars to keep prompt size bounded.

    Order: blocks before the picture first, then blocks after, in
    document order. This puts the section heading + introductory
    paragraph closest to the prompt's grounding, mirroring how a
    human reads the figure.
    """
    pic = blocks[pic_idx]
    pic_page = pic.get("page", 0)
    pic_section = (pic.get("section") or "").strip()
    page_min, page_max = pic_page - CTX_PAGES, pic_page + CTX_PAGES

    def is_textish(b):
        return b.get("type") in (None, "text", "heading", "table", "code") \
               and (b.get("text") or "").strip()

    same_section: list[dict] = []
    nearby: list[dict] = []
    for i, b in enumerate(blocks):
        if i == pic_idx:
            continue
        if not is_textish(b):
            continue
        bp = b.get("page", 0)
        if not (page_min <= bp <= page_max):
            continue
        bs = (b.get("section") or "").strip()
        if pic_section and bs == pic_section:
            same_section.append(b)
        nearby.append(b)

    # Prefer same-section; if too thin (<500 chars), augment with nearby.
    pool = same_section if sum(len(b["text"]) for b in same_section) >= 500 \
                        else nearby

    # Truncate, joining with paragraph breaks.
    chunks: list[str] = []
    total = 0
    for b in pool:
        t = b["text"].strip()
        if not t:
            continue
        if total + len(t) + 2 > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                chunks.append(t[:remaining] + "[…ctx-truncated]")
                total = max_chars
            break
        chunks.append(t)
        total += len(t) + 2
    return "\n\n".join(chunks)


def _ollama_caption(image_bytes: bytes, prompt: str, timeout: int = 300) -> tuple[str, float]:
    """POST a single image+prompt to Ollama /api/chat and return
    (caption, elapsed_seconds)."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": VLM_MODEL,
        "stream": False,
        "messages": [
            {"role": "user", "content": prompt, "images": [b64]},
        ],
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}") from e
    dt = time.time() - t0
    raw = (payload.get("message", {}) or {}).get("content", "") or ""
    return raw.strip(), dt


def _restrip_sidecar(sidecar_path: Path) -> dict:
    """Re-run the meta-stripper over every picture block's stored
    vlm_description_raw. Cheap follow-up after a stripper-pattern
    improvement — no VLM calls needed. Updates vlm_description + text
    fields. Skips blocks with no raw output to re-process."""
    d = json.loads(sidecar_path.read_text("utf-8"))
    blocks = d.get("blocks", [])
    changed = 0
    for b in blocks:
        if b.get("type") != "picture":
            continue
        raw = b.get("vlm_description_raw")
        if not raw:
            continue
        new_stripped = strip_caption(raw)
        if new_stripped == b.get("vlm_description"):
            continue
        b["vlm_description"] = new_stripped
        caption = (b.get("caption") or "").strip()
        if caption and new_stripped:
            b["text"] = f"{caption}\n\n{new_stripped}"
        elif new_stripped:
            b["text"] = new_stripped
        elif caption:
            b["text"] = caption
        changed += 1
    if changed:
        sidecar_path.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    return {"file": sidecar_path.name, "restripped": changed}


def _process_sidecar(sidecar_path: Path, collection_folder: Path,
                     force: bool) -> dict:
    """Caption every picture block in one sidecar. Returns a stats dict."""
    d = json.loads(sidecar_path.read_text("utf-8"))
    blocks = d.get("blocks", [])
    pics = [(i, b) for i, b in enumerate(blocks) if b.get("type") == "picture"]
    if not pics:
        return {"file": sidecar_path.name, "pics": 0, "captioned": 0,
                "skipped": 0, "errors": 0, "elapsed": 0.0}

    captioned = 0
    skipped = 0
    errors = 0
    elapsed = 0.0
    pic_count = len(pics)

    for nth, (pic_idx, pic) in enumerate(pics, 1):
        if pic.get("vlm_description") and not force:
            skipped += 1
            continue

        img_rel = pic.get("image_path", "")
        if not img_rel:
            print(f"    [{sidecar_path.stem}] p.{pic.get('page')} pic{nth}: "
                  f"no image_path, skipping", flush=True)
            errors += 1
            continue
        img_abs = (collection_folder / img_rel).resolve()
        if not img_abs.is_file():
            print(f"    [{sidecar_path.stem}] p.{pic.get('page')} pic{nth}: "
                  f"image missing at {img_abs}, skipping", flush=True)
            errors += 1
            continue

        context = _build_context(blocks, pic_idx, CTX_CHARS)
        prompt = PROMPT_V2_CTX_TEMPLATE.format(context=context)
        try:
            raw, dt = _ollama_caption(img_abs.read_bytes(), prompt)
        except Exception as exc:
            print(f"    [{sidecar_path.stem}] p.{pic.get('page')} pic{nth}: "
                  f"VLM error {exc}", flush=True)
            errors += 1
            continue

        stripped = strip_caption(raw)
        # Persist BOTH raw and stripped so a future stripper improvement
        # can re-process existing captions without spending VLM time.
        # Field naming: vlm_description = current best (stripped); same
        # field continues to drive the `text` field for Chroma chunking.
        pic["vlm_description"] = stripped
        pic["vlm_description_raw"] = raw
        pic["vlm_model"] = VLM_MODEL
        pic["vlm_prompt_id"] = "v2-ctx"
        # Re-build the chunkable text: caption + description, both
        # present so the RAG chunk is searchable on either signal.
        caption = (pic.get("caption") or "").strip()
        if caption and stripped:
            pic["text"] = f"{caption}\n\n{stripped}"
        elif stripped:
            pic["text"] = stripped
        elif caption:
            pic["text"] = caption
        captioned += 1
        elapsed += dt
        print(f"    [{sidecar_path.stem}] p.{pic.get('page')} pic{nth}/{pic_count}: "
              f"{dt:.1f}s, raw {len(raw)} -> stripped {len(stripped)} chars",
              flush=True)

        # Checkpoint: flush JSON every CHECKPOINT_EVERY captioned blocks
        # so a crash mid-file doesn't lose accumulated work. The skip-
        # if-already-captioned check at the top of the loop makes
        # restart cheap — already-captioned blocks are O(1) skipped.
        if captioned % CHECKPOINT_EVERY == 0:
            sidecar_path.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
            print(f"    [{sidecar_path.stem}] checkpoint: "
                  f"{captioned} captioned, persisted to disk", flush=True)

    # Final write for whatever's left since the last checkpoint.
    if captioned:
        sidecar_path.write_text(json.dumps(d, ensure_ascii=False), "utf-8")

    return {"file": sidecar_path.name, "pics": pic_count,
            "captioned": captioned, "skipped": skipped, "errors": errors,
            "elapsed": elapsed}


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    force = "--force" in argv
    restrip_only = "--restrip-only" in argv
    only_idx = next((i for i, a in enumerate(argv) if a == "--only"), None)
    only_pdf = argv[only_idx + 1] if only_idx is not None else None

    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    data_dir = Path(args[0]).resolve()
    if not data_dir.is_dir():
        print(f"data dir not found: {data_dir}", file=sys.stderr)
        return 2
    if len(args) > 1:
        collections = [args[1]]
    else:
        collections = sorted(
            e.name for e in data_dir.iterdir()
            if e.is_dir() and not e.name.startswith(".")
        )

    overall_start = time.time()
    overall = {"pics": 0, "captioned": 0, "skipped": 0, "errors": 0}

    for collection in collections:
        folder = data_dir / collection
        cache = folder / ".rag-cache"
        if not cache.is_dir():
            print(f"[{collection}] no .rag-cache — skip")
            continue
        sidecars = sorted(cache.glob("*.json"))
        if only_pdf:
            sidecars = [s for s in sidecars if s.name == f"{only_pdf}.json"
                        or s.stem == only_pdf]
        print(f"[{collection}] {len(sidecars)} sidecar(s) to scan", flush=True)
        for s in sidecars:
            if restrip_only:
                rs = _restrip_sidecar(s)
                if rs["restripped"]:
                    print(f"[{collection}] {rs['file']}: "
                          f"re-stripped {rs['restripped']} block(s)", flush=True)
                continue
            stats = _process_sidecar(s, folder, force)
            overall["pics"] += stats["pics"]
            overall["captioned"] += stats["captioned"]
            overall["skipped"] += stats["skipped"]
            overall["errors"] += stats["errors"]
            if stats["pics"]:
                avg = stats["elapsed"] / stats["captioned"] if stats["captioned"] else 0
                print(f"[{collection}] {stats['file']}: "
                      f"{stats['captioned']}/{stats['pics']} captioned "
                      f"({stats['skipped']} skipped, {stats['errors']} errors, "
                      f"{stats['elapsed']:.0f}s, {avg:.1f}s/pic avg)",
                      flush=True)

    overall_elapsed = time.time() - overall_start
    print(f"\nDone. pictures={overall['pics']} "
          f"captioned={overall['captioned']} "
          f"skipped={overall['skipped']} "
          f"errors={overall['errors']} "
          f"wall={overall_elapsed:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
