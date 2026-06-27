#!/usr/bin/env python3
"""
Rebuild `section` metadata from a sidecar's own heading blocks.

For PDFs that ship no usable bookmark outline (or a degenerate one — e.g. a
single title-page bookmark), `_apply_toc` in extract.py collapses every block
to one flat section (the running-header / document title). That kills citation
granularity and disables the rag_search `section_filter` two-pass feature for
that document.

This remediation walks each sidecar's blocks in document order and reconstructs
a running clause section from the Nemotron/Parse `type:"heading"` blocks that
Parse already detected — no re-extraction needed, so existing VLM captions are
preserved. The section anchor is a clause-numbered heading ("8.2.1.1 RCH
Downstream Port RCRB"); a bare clause number ("3.2.4.5.6") is merged with the
immediately following title heading where present. Non-numbered headings (page
running-titles, "Prerequisites:", stray captions) do NOT reset the section, so
running-header noise can't pollute it.

SAFE BY DESIGN: a sidecar is only rewritten when its current section metadata is
degenerate (<= --max-existing-sections distinct non-empty values). Well-
bookmarked sidecars (CXL 3.2 has ~1000 sections) are left untouched, so this can
be run against a whole collection.

Usage:
    python resection-from-headings.py <data_dir> <collection> [--only <pdf>]
                                       [--max-existing-sections 3] [--dry]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# A clause number at the start of a heading: "8.2.1.1", "9.11", "3.2.4.5.6",
# optionally followed by the clause title on the same line.
_CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:\s+(.*\S))?\s*$")


def _clean(s: str) -> str:
    return " ".join(str(s or "").split())


def resection_blocks(blocks: list[dict]) -> int:
    """Mutate blocks in place; return the number whose section changed."""
    current = ""           # running clause section assigned to following blocks
    pending_num = None     # a bare clause number awaiting its title heading
    changed = 0
    for b in blocks:
        if b.get("type") == "heading":
            txt = _clean(b.get("text"))
            m = _CLAUSE_RE.match(txt)
            if m:
                num, title = m.group(1), m.group(2)
                if title:                      # "8.2.1.1 RCH Downstream Port RCRB"
                    current = f"{num} {title}"
                    pending_num = None
                else:                          # bare "3.2.4.5.6" — await its title
                    pending_num = num
                    current = num
            elif pending_num:                  # title heading right after a bare number
                current = f"{pending_num} {txt}"
                pending_num = None
            # non-numbered, non-pending heading: leave `current` as-is (noise guard)
        # assign the running section to every block (headings included)
        new_section = current
        if new_section and b.get("section") != new_section:
            b["section"] = new_section
            b["section_path"] = new_section
            changed += 1
    return changed


def distinct_sections(blocks: list[dict]) -> int:
    return len({_clean(b.get("section")) for b in blocks if _clean(b.get("section"))})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir")
    ap.add_argument("collection")
    ap.add_argument("--only", default=None, help="restrict to one sidecar (PDF stem or filename substring)")
    ap.add_argument("--max-existing-sections", type=int, default=3,
                    help="only resection sidecars with <= this many distinct sections (degenerate)")
    ap.add_argument("--dry", action="store_true", help="report only; do not write")
    args = ap.parse_args()

    cache = Path(args.data_dir) / args.collection / ".rag-cache"
    if not cache.is_dir():
        print(f"no .rag-cache at {cache}", file=sys.stderr)
        return 1

    sidecars = sorted(cache.glob("*.json"))
    if args.only:
        sidecars = [p for p in sidecars if args.only in p.name]
    if not sidecars:
        print("no matching sidecars", file=sys.stderr)
        return 1

    for p in sidecars:
        d = json.loads(p.read_text(encoding="utf-8"))
        blocks = d.get("blocks", [])
        before = distinct_sections(blocks)
        if before > args.max_existing_sections:
            print(f"  SKIP {p.name}: {before} distinct sections (already well-sectioned)")
            continue
        heads = sum(1 for b in blocks if b.get("type") == "heading")
        changed = resection_blocks(blocks)
        after = distinct_sections(blocks)
        print(f"  {'DRY ' if args.dry else ''}{p.name}: {before} -> {after} sections "
              f"from {heads} headings ({changed} blocks relabelled)")
        if not args.dry:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
