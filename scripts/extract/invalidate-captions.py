#!/usr/bin/env python3
"""
Invalidate VLM captions on picture blocks so a future captioner run
re-generates them.

For each sidecar in the chosen collection(s), walks every type:"picture"
block and clears: vlm_description, vlm_description_raw, vlm_model,
vlm_prompt_id. The `text` field is reset to the figure caption alone
(or empty if there's none) so the rendered .md doesn't carry stale
description content.

Usage:
    python invalidate-captions.py <data_dir> [collection] [--dry]
    python invalidate-captions.py <data_dir> [collection] --only-prompt v2-ctx

By default invalidates every captioned picture. With --only-prompt PID,
only blocks whose stored vlm_prompt_id matches PID are cleared (useful
when transitioning prompts mid-corpus).

WARNING: this is destructive. The cleared captions cost ~60s GPU each
to regenerate. Use --dry first.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


CLEAR_KEYS = ("vlm_description", "vlm_description_raw",
              "vlm_model", "vlm_prompt_id")


def _invalidate_sidecar(sidecar_path: Path, dry: bool,
                        only_prompt: str | None) -> tuple[int, int]:
    """Returns (n_cleared, n_total_pics)."""
    data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    blocks = data.get("blocks", [])
    cleared = 0
    n_pics = 0
    for b in blocks:
        if b.get("type") != "picture":
            continue
        n_pics += 1
        if not (b.get("vlm_description") or b.get("vlm_description_raw")):
            continue
        if only_prompt and b.get("vlm_prompt_id") != only_prompt:
            continue
        for k in CLEAR_KEYS:
            if k in b:
                b[k] = ""
        caption = (b.get("caption") or "").strip()
        b["text"] = caption
        cleared += 1
    if cleared and not dry:
        sidecar_path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return cleared, n_pics


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    argv = sys.argv[1:]
    dry = "--dry" in argv
    argv = [a for a in argv if a != "--dry"]

    only_prompt: str | None = None
    if "--only-prompt" in argv:
        i = argv.index("--only-prompt")
        only_prompt = argv[i + 1]
        del argv[i:i + 2]

    data_dir = Path(argv[0]).resolve()
    if not data_dir.is_dir():
        print(f"! data_dir does not exist: {data_dir}")
        return 2
    only_collection = argv[1] if len(argv) > 1 else None

    if only_collection:
        cols = [data_dir / only_collection]
    else:
        cols = sorted(p for p in data_dir.iterdir()
                      if p.is_dir() and (p / ".rag-cache").is_dir())

    print(f"dry={'YES' if dry else 'no'}  "
          f"only_prompt={only_prompt or '(any)'}")
    total_cleared = 0
    total_pics = 0
    for col in cols:
        cache = col / ".rag-cache"
        if not cache.is_dir():
            print(f"skip {col.name}: no .rag-cache")
            continue
        print(f"\n== {col.name} ==")
        for sc in sorted(cache.glob("*.json")):
            c, p = _invalidate_sidecar(sc, dry, only_prompt)
            if c:
                print(f"  {sc.name}: cleared {c}/{p}")
            total_cleared += c
            total_pics += p

    verb = "would clear" if dry else "cleared"
    print(f"\nDONE  {verb}  {total_cleared}/{total_pics} pictures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
