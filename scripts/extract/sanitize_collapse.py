#!/usr/bin/env python3
"""
Token-collapse sanitizer for Nemotron Parse v1.2 output.

Parse occasionally enters a generation runaway loop where a short token
(or short phrase) is regenerated hundreds of times until num_predict
or end-of-context. The 2026-05-25 scan of data/ieee/.rag-cache/ found
8 affected blocks across 7 IEEE specs:

  - 8021AB-2016 p.1 cover: `(S)` × 790 + fake `@gmail.com` × 99
  - 8021AS-2025 p.336: `\\------` cascade (22 KB)
  - 8021CBcv-2021 p.48, p.74: ` ... ` cascade (18 KB each)
  - 8021CBdb-2021 p.43, p.48: ` ``` ` cascade (23-27 KB)
  - 8021Qat-2010 p.99: ` ... ` cascade (17 KB)
  - 8021Qci-2017 p.43: large block (needs verification)
  - 8021Qcw-2023 p.92: large block (needs verification)

Every one of these is currently indexed in Chroma, returning fabricated
content for any query that lands on the affected chunks.

This module:
  - `detect_collapse(text)` -> int|None: returns char offset where
    cascade starts, or None for clean text.
  - `sanitize_block(text)` -> tuple[str, bool]: returns
    (cleaned_text, was_truncated).
  - `sanitize_sidecar(sidecar)` -> tuple[dict, list]: returns
    (modified_sidecar, list_of_changes) where changes log per-block
    truncations for audit.

Used in two places:
  1. One-shot via __main__: scan + sanitize all sidecars in a directory.
  2. Imported by extract-nemo.py: applied to every block before the
     JSON is written, so new extractions never carry the cascade.

Pure stdlib regex. Idempotent (re-sanitizing clean text is a no-op).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


# ─── Detection ────────────────────────────────────────────────────────

# Patterns derived from the 2026-05-25 IEEE Parse scan. Each catches a
# specific observed runaway shape. Order matters only for the earliest-
# offset wins logic below.

_COLLAPSE_PATTERNS = [
    # Whitespace-separated short token repeating many times.
    # Covers `\------ \------ \------ ...` (8021AS) and similar shapes.
    re.compile(r"(\S{1,12})(?:\s+\1){15,}"),

    # Adjacent same-substring repetition (no whitespace required).
    # Covers ` ``` ``` ``` ... ` (8021CBdb) and any contiguous cascade.
    re.compile(r"(.{3,12}?)\1{10,}", flags=re.DOTALL),

    # Dot-leader cascade ("... ... ... ..."). Distinct enough to warrant
    # its own pattern — the dot is short enough that the generic
    # patterns above can miss it on tight clustering.
    re.compile(r"(?:\.\s*){50,}"),

    # `(S)` cascade specifically — covers the IEEE cover-page failure
    # where the model hallucinated emails and then collapsed to (S).
    re.compile(r"\(S\)[\s\\()]*?(?:\(S\)[\s\\()]*?){15,}"),

    # "Electronic address: ..." appearing 3+ times — this is a strong
    # signal that Parse hallucinated author entries from an IEEE cover-
    # page logo. Real IEEE specs do not list multiple back-to-back
    # author email addresses anywhere in their body text. We match the
    # FIRST occurrence so truncation drops the whole hallucinated block.
    # Match shape: "<sup>∗</sup>Electronic address: ..." OR
    # "\(^\unknown\)Electronic address: ..." — anything followed by
    # "Electronic address:" repeated.
    re.compile(
        r"(?:<sup>[^<]{0,10}</sup>|\\\(\^[^)]{0,20}\\?\))?\s*Electronic address:.{0,150}?"
        r"(?:<sup>[^<]{0,10}</sup>|\\\(\^[^)]{0,20}\\?\))?\s*Electronic address:.{0,150}?"
        r"(?:<sup>[^<]{0,10}</sup>|\\\(\^[^)]{0,20}\\?\))?\s*Electronic address:",
        flags=re.DOTALL,
    ),
]


def detect_collapse(text: str) -> int | None:
    """Return the earliest character offset where a runaway-token
    cascade begins in `text`, or None if no cascade detected.

    A cascade is recognised as any of the patterns in _COLLAPSE_PATTERNS
    matching. Multiple patterns may match in different positions; we
    return the EARLIEST so the truncation removes everything from the
    first cascade onward (even if a later cascade was matched by a
    different pattern).
    """
    # Min-length guard: a short block can't plausibly have a 15-token
    # cascade hidden inside legitimate prose, so the patterns are safe
    # to apply. The guard exists to avoid false-positives on very short
    # captions/headings — 200 chars is enough to absorb that risk while
    # still catching small Parse-mangled blocks.
    if len(text) < 200:
        return None
    earliest: int | None = None
    for pat in _COLLAPSE_PATTERNS:
        m = pat.search(text)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    return earliest


def sanitize_block(text: str) -> tuple[str, bool]:
    """Truncate `text` at the first detected cascade. Returns
    (cleaned_text, was_truncated). Cleaned text gets a trailing
    `[…collapse-truncated by sanitize_collapse]` marker so any
    downstream reader (Chroma chunk inspector, .md previewer) can
    distinguish truncation from natural end-of-block."""
    cut = detect_collapse(text)
    if cut is None:
        return text, False
    # Trim trailing whitespace before the marker so the boundary
    # reads cleanly.
    kept = text[:cut].rstrip()
    marker = " […collapse-truncated by sanitize_collapse]"
    return kept + marker, True


def sanitize_sidecar(sidecar: dict) -> tuple[dict, list[dict]]:
    """Walk `sidecar["blocks"]`, apply sanitize_block to each block's
    text. Returns (modified_sidecar_dict, change_log) where change_log
    is a list of dicts describing each truncation:
        {"block_id": "...", "page": N, "before_len": M, "after_len": K}

    The sidecar dict is modified in-place AND returned for convenience.
    Idempotent: clean sidecars come back unchanged with an empty log.
    """
    changes: list[dict] = []
    for b in sidecar.get("blocks", []):
        orig = b.get("text", "")
        if not orig:
            continue
        cleaned, was_truncated = sanitize_block(orig)
        if was_truncated:
            changes.append({
                "block_id": b.get("id", "?"),
                "page": b.get("page", 0),
                "before_len": len(orig),
                "after_len": len(cleaned),
            })
            b["text"] = cleaned
    return sidecar, changes


# ─── CLI ──────────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    """Sanitize every *.json file in a directory. Writes changes back
    in-place. Logs per-file change counts to stdout."""
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(argv[0])
    if target.is_dir():
        files = sorted(target.glob("*.json"))
    elif target.is_file():
        files = [target]
    else:
        print(f"not a file or directory: {target}", file=sys.stderr)
        return 2
    total_truncated = 0
    for f in files:
        try:
            sidecar = json.loads(f.read_text("utf-8"))
        except Exception as exc:
            print(f"[skip] {f.name}: cannot parse JSON ({exc})", file=sys.stderr)
            continue
        if sidecar.get("backend") not in ("nemotron-parse-v1.2",):
            # Only Parse sidecars exhibit token collapse; skip others
            # silently so this can run over a mixed directory.
            continue
        _, changes = sanitize_sidecar(sidecar)
        if changes:
            f.write_text(json.dumps(sidecar, ensure_ascii=False), "utf-8")
            print(f"[fix] {f.name}: {len(changes)} block(s) truncated")
            for c in changes:
                saved = c["before_len"] - c["after_len"]
                print(f"        block {c['block_id']} (p.{c['page']}): "
                      f"{c['before_len']} -> {c['after_len']} chars "
                      f"(-{saved})")
            total_truncated += len(changes)
        else:
            print(f"[ok]  {f.name}: clean")
    print(f"\nDone. {total_truncated} block(s) truncated across {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
