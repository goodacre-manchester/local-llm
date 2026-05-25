#!/usr/bin/env python3
"""
Render .rag-cache JSON sidecars as readable Markdown for VS Code preview.

Reads  : data/<collection>/.rag-cache/<pdf>.json
Writes : data/<collection>/.rag-md/<pdf>.md   (gitignored under data/*/)

For each JSON sidecar, emits a Markdown view of its blocks so the source can
be skimmed in an IDE (Ctrl+Shift+V opens the preview). Headings drive the
outline panel; tables are rendered as real tables (the JSON's `type=table`
blocks already hold valid markdown table syntax); page boundaries appear as
HTML comments (invisible in preview, navigable in source view).

Idempotent: an .md is regenerated only when it is missing OR older than its
JSON. Pass --force to rebuild all of them. Safe to run while extraction is
in flight — newly-completed sidecars get rendered on the next invocation.

No heavy dependencies — pure stdlib, runs in any Python 3.10+ (including
WSL's system python3).

Usage:
    python dump-sidecar-md.py <data_dir>                # every collection
    python dump-sidecar-md.py <data_dir> <collection>   # one collection
    python dump-sidecar-md.py <data_dir> ieee --force   # rebuild even if up-to-date
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _emit_block(block: dict, lines: list[str], state: dict) -> None:
    """Append one block's markdown to `lines`. `state` carries the previous
    page / section so transitions only emit a marker on change."""
    page    = block.get("page") or 0
    section = (block.get("section") or "").strip()
    btype   = block.get("type", "text")
    text    = (block.get("text") or "").strip()

    if not text:
        return

    # Page boundary — invisible in rendered preview, jumpable in source view.
    if page > 0 and page != state.get("page"):
        if state.get("page") is not None:
            lines.append("")
        lines.append(f"<!-- p.{page} -->")
        lines.append("")
        state["page"] = page

    # Section boundary — markdown level-2 header so the outline panel reads
    # like a table of contents. Standards-PDF section text comes from the
    # bookmark tree via _apply_toc(), so it's the clause-exact title.
    if section and section != state.get("section"):
        lines.append("")
        lines.append(f"## §{section}")
        lines.append("")
        state["section"] = section

    if btype == "heading":
        # On-page heading text. _md_page_to_blocks() stripped the leading
        # `#`s, so re-add as level-3 (nested under the section's level-2).
        # If it's identical to the current section heading we'd just-emitted,
        # skip to avoid the duplicate that clause-titled standards produce.
        if text == section:
            return
        lines.append(f"### {text}")
        lines.append("")
    elif btype == "table":
        # text is already a markdown table — preserved whole by
        # _md_page_to_blocks(). Pass through.
        lines.append(text)
        lines.append("")
    else:
        # Paragraph. text already carries inline markdown (bold/sup/<br>) so
        # the preview renders it directly.
        lines.append(text)
        lines.append("")


def render_sidecar(sidecar: dict) -> str:
    doc     = sidecar.get("doc", "(unknown)")
    backend = sidecar.get("backend", "?")
    blocks  = sidecar.get("blocks", [])

    lines: list[str] = [
        f"# {doc}",
        f"<!-- backend: {backend} — {len(blocks)} blocks -->",
        "",
    ]
    state: dict = {"page": None, "section": None}
    for b in blocks:
        _emit_block(b, lines, state)

    # Collapse runs of blank lines for readability.
    out: list[str] = []
    prev_blank = False
    for ln in lines:
        is_blank = ln == ""
        if is_blank and prev_blank:
            continue
        out.append(ln)
        prev_blank = is_blank
    return "\n".join(out).rstrip() + "\n"


def process_collection(folder: Path, force: bool) -> tuple[int, int]:
    """Render every sidecar in <folder>/.rag-cache/*.json.
    Returns (written, skipped)."""
    cache = folder / ".rag-cache"
    if not cache.is_dir():
        return (0, 0)

    md_dir = folder / ".rag-md"
    md_dir.mkdir(exist_ok=True)

    written = skipped = 0
    for j in sorted(cache.glob("*.json")):
        # foo.pdf.json -> foo.md   (drop both extensions for the .md name)
        out_name = j.stem
        if out_name.lower().endswith(".pdf"):
            out_name = out_name[: -len(".pdf")]
        out = md_dir / f"{out_name}.md"

        if not force and out.exists() and out.stat().st_mtime >= j.stat().st_mtime:
            skipped += 1
            continue

        try:
            sidecar = json.loads(j.read_text("utf-8"))
        except Exception as exc:
            print(f"  [{folder.name}] ERROR reading {j.name}: {exc}",
                  file=sys.stderr, flush=True)
            continue

        md = render_sidecar(sidecar)
        out.write_text(md, "utf-8")
        n_blocks = len(sidecar.get("blocks", []))
        print(f"  [{folder.name}] -> .rag-md/{out.name}  "
              f"({len(md):,} chars, {n_blocks} blocks)", flush=True)
        written += 1

    if written == 0 and skipped > 0:
        print(f"  [{folder.name}] all {skipped} sidecars up to date", flush=True)
    return (written, skipped)


def main(argv: list[str]) -> int:
    args  = [a for a in argv if not a.startswith("--")]
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

    total_w = total_s = 0
    for c in collections:
        folder = data_dir / c
        if not folder.is_dir():
            print(f"[{c}] not a directory; skipping", file=sys.stderr)
            continue
        w, s = process_collection(folder, force)
        total_w += w
        total_s += s

    print(f"Done. {total_w} rendered, {total_s} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
