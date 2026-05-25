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
import re
import sys
from collections import Counter
from pathlib import Path


# Nemotron Parse converts ASCII-art boxes / dotted-leader TOCs / spec tables
# into bare LaTeX-tabular syntax (no `\begin{tabular}{...}` preamble — just
# `\begin`, cells joined by `&`, rows terminated by `\` or `\\`, closing
# `\end`). Markdown previewers can't render that. Convert to a proper GFM
# table when the structure is consistent, or to a bullet list when it
# isn't, falling back to a fenced code block for unparseable shapes.
# `\begin` may be followed by zero or more `{...}` preamble groups
# (e.g. `\begin{tabular}{cc}` is two groups). Match all of them so the
# captured body excludes the LaTeX preamble — otherwise it leaks into
# the first cell. `\end` likewise may be followed by `{tabular}` etc.
_LATEX_TABULAR = re.compile(
    r"\\begin(?:\s*\{[^}]*\})*\s*(.*?)\\end(?:\s*\{[^}]*\})*",
    flags=re.DOTALL,
)
# Decorative ASCII-art row: cells that are entirely +, -, |, or whitespace.
_DECOR_CELL = re.compile(r"^[+\-|\s]*$")

# Parse sometimes emits LaTeX-table content WITHOUT the surrounding
# \begin / \end markers — a bare `{tabular}{ccc}` preamble at the start
# of a block, plus inline `\multicolumn` / `\multirow` commands. These
# bypass the _LATEX_TABULAR converter because there's no environment to
# match. Strip the formatting commands so the underlying content reads
# as plain prose / em-dash-separated rows.
_ORPHAN_TABULAR_PREAMBLE = re.compile(
    r"\{(?:tabular|tabularx|array|longtable|table|matrix)\}"
    r"(?:\s*\{[^}]*\})+"
)
# `\multicolumn{N}{spec}{content}` and `\multirow{N}{spec}{content}` —
# both 3-argument forms. Keep the content (the 3rd `{...}` group).
# Allow extra `\` prefix because Parse sometimes double-escapes.
_LATEX_MULTI = re.compile(
    r"\\+(?:multicolumn|multirow)\s*\{[^}]*\}\s*\{[^}]*\}\s*\{([^}]*)\}"
)
# Fallback for malformed cases (nested braces Parse occasionally emits,
# e.g. `\multirow{2{*}{}}`). Strip the command + everything up to the
# next em-dash or end-of-line.
_LATEX_MULTI_MALFORMED = re.compile(
    r"\\+(?:multicolumn|multirow)\b[^\n—]*?(?=—|$|\n)",
    flags=re.MULTILINE,
)

# Pattern for clause-numbered headings ("11.4 Title", "12.29.1.1 Title").
# Used to derive markdown heading level from the heading text itself
# rather than the page-granular `section` field (which _apply_toc()
# over-assigns when multiple clauses share a page — every block on the
# shared page gets tagged with whichever clause starts last, so blocks
# from EARLIER clauses on that page falsely match the section field).
_CLAUSE_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)\b")


def _heading_level(text: str) -> int:
    """Map a heading's clause-number depth to a markdown heading level.
    `11` or `11.4` → 2; `11.3.4` → 3; `11.4.1.2.3` → 4 (capped).
    Non-clause headings (Annex titles, 'Abstract', 'TABLE OF CONTENTS')
    default to level 3 — visible but not competing with clause level-2."""
    stripped = text.lstrip("*_# ").strip()
    m = _CLAUSE_NUM.match(stripped)
    if not m:
        return 3
    depth = m.group(1).count(".") + 1
    if depth <= 2:
        return 2
    if depth == 3:
        return 3
    return 4


# ─── ASN.1 / SMIv2 MIB module detection + reformatting ───────────────────────
# Parse reads source code regions of standards PDFs (MIB definitions,
# state-machine pseudocode) as flowed text — a single block whose `text`
# field is hundreds of characters with no newlines. Detect and wrap as a
# fenced code block so VS Code Markdown Preview shows monospace + syntax
# (also tells the RAG embedder this is code, not prose).
_ASN1_MARKERS = re.compile(
    # Module declaration (strong signal — only in MIB headers)
    r"\bDEFINITIONS\s*::=\s*BEGIN\b"
    # SMIv2 macros used as object definitions
    r"|\b(?:MODULE-IDENTITY|MODULE-COMPLIANCE|TEXTUAL-CONVENTION|"
    r"OBJECT-IDENTITY|NOTIFICATION-TYPE|OBJECT-TYPE|OBJECT-GROUP|"
    r"NOTIFICATION-GROUP)\b\s+\w"
    # OID assignments (continuation-block signal — common in MIB body
    # without any module-declaration nearby)
    r"|::=\s*\{[^}]*\}"
    r"|\bOBJECT IDENTIFIER\b"
    # SYNTAX <type> declarations
    r"|\bSYNTAX\s+(?:INTEGER|OCTET STRING|SEQUENCE|BIT STRING|"
    r"DisplayString|Counter32|Gauge32|Unsigned32|TimeTicks|IpAddress)\b"
    # ASN.1 line-start comment (heuristic — line begins with `--` followed
    # by space + uppercase or asterisk — rare in plain prose)
    r"|^\s*--\s+[A-Z*]",
    flags=re.MULTILINE,
)
# Keywords that should each start their own line in a well-formed
# SMIv2 module. Insert a newline before them when found mid-flow.
_ASN1_LINE_KEYWORDS = re.compile(
    r"\s+(?=(?:"
    r"MODULE-IDENTITY|MODULE-COMPLIANCE|TEXTUAL-CONVENTION|OBJECT-IDENTITY|"
    r"OBJECT-TYPE|NOTIFICATION-TYPE|OBJECT-GROUP|NOTIFICATION-GROUP|"
    r"LAST-UPDATED|ORGANIZATION|CONTACT-INFO|DESCRIPTION|REVISION|"
    r"REFERENCE|STATUS|SYNTAX|MAX-ACCESS|MIN-ACCESS|ACCESS|UNITS|"
    r"AUGMENTS|INDEX|DEFVAL|BEGIN|END|FROM|IMPORTS)\s+\S"
    r")"
)


def _reformat_asn1(code: str) -> str:
    """Insert line breaks at SMIv2 statement boundaries and squeeze runs
    of spaces. Conservative — only breaks at common keywords; doesn't try
    to fully pretty-print (would need a grammar)."""
    out = _ASN1_LINE_KEYWORDS.sub("\n", code)
    out = re.sub(r"  +", " ", out)
    return out.strip()


# Statistical page-chrome detection. Identifies recurring page
# header / footer content (publisher attribution, license, copyright,
# document title-bars) by repetition near page boundaries, without
# hard-coding any specific publisher's wording — PDF-source-agnostic.
_PAGE_MARKER = re.compile(r"^<!-- p\.\d+ -->$")


def _normalize_chrome_line(s: str) -> str:
    """Aggressive normalization so trivially-varying page-header lines
    collapse to the same bucket: lowercase, unify dash variants, drop
    all digits and trailing punctuation, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"[—–\-]+", "-", s)         # unify Unicode dash variants
    s = re.sub(r"\d+", "", s)                # drop digits (page nums, dates, IDs)
    s = re.sub(r"\s+", " ", s).strip()       # collapse whitespace runs
    s = s.strip("-.,;:|/ ")                  # strip trailing punctuation
    return s


def _detect_page_chrome(lines: list[str]) -> set[str]:
    """Identify lines that recur near page-marker boundaries — i.e. page
    headers / footers. Returns a set of normalized strings; callers
    normalize each candidate line and check membership.

    Threshold tuned to catch chrome that appears on ~20% of pages —
    enough to grab true headers/footers without false-positiving
    legitimately-recurring section titles or boilerplate that only
    appears on a few pages."""
    candidates: dict[str, int] = {}
    window = 4
    marker_pos = [i for i, ln in enumerate(lines) if _PAGE_MARKER.match(ln.strip())]
    if len(marker_pos) < 3:
        return set()
    for pos in marker_pos:
        for j in range(max(0, pos - window), min(len(lines), pos + window + 1)):
            if j == pos:
                continue
            s = lines[j].strip()
            if not s or len(s) > 500:
                continue
            norm = _normalize_chrome_line(s)
            if not norm or len(norm) < 8:
                continue  # too short to be a meaningful chrome match
            candidates[norm] = candidates.get(norm, 0) + 1
    threshold = max(2, len(marker_pos) // 5)
    return {n for n, c in candidates.items() if c >= threshold}


def _is_page_chrome(line: str, chrome_norms: set[str] | None = None) -> bool:
    """Is this line chrome? Page markers always are; arbitrary text is
    chrome only if its normalized form is in the corpus-detected set."""
    s = line.strip()
    if not s:
        return False
    if _PAGE_MARKER.match(s):
        return True
    if chrome_norms is None:
        return False
    return _normalize_chrome_line(s) in chrome_norms


# Long ASCII-art divider lines (banner-comment rows of `*`, `-`, `=`, etc.)
# render as enormous single lines in markdown preview. Collapse runs of
# 30+ identical characters to 20 — preserves the divider's visual intent
# without the horizontal-scroll penalty.
_LONG_RUN = re.compile(r"(.)\1{29,}")


def _collapse_long_runs(text: str) -> str:
    return _LONG_RUN.sub(lambda m: m.group(1) * 20, text)


def _is_code_continuation_orphan(line: str) -> bool:
    """An orphan line between code fences that's actually a continuation
    of MIB content (e.g. a stranded DESCRIPTION string fragment) and
    should be absorbed into the consolidated code block."""
    s = line.strip()
    if not s:
        return False
    if s.startswith('"') or s.endswith('"'):
        return True
    if "::=" in s or s.startswith("--") or s.startswith("{") or s.endswith("}"):
        return True
    return False


_MD_HEADING = re.compile(r"^\s*#{1,6}\s+\S")


def _consolidate_code_fences(md: str) -> str:
    """Consolidate Parse-fragmented MIB modules into one continuous code
    fence per module.

    Two-pass strategy:

    1. **BEGIN-anchored scope**: when a code fence contains
       `DEFINITIONS ::= BEGIN`, everything from that fence until the
       next markdown heading (clause boundary) belongs to that one MIB
       module. Absorb all intervening content — fence markers, page
       chrome, orphan DESCRIPTION-string fragments, prose paragraphs
       (which inside SMIv2 are usually multi-paragraph DESCRIPTION
       string bodies broken across pages by Parse) — into one fence.
       The chrome (IEEE Std headers, license footers, page markers) is
       dropped; everything else lands as code.

    2. **Non-module fence merging**: for code fences NOT inside a
       BEGIN-scoped module (smaller OID-assignment fragments, banner
       comments, etc.), apply the simpler chrome-only merge — adjacent
       fences separated by chrome only become one fence.

    Also unescapes markdown-escaped asterisks (`\\*` → `*`) inside the
    consolidated fence so MIB banner-comments render literally."""
    lines = md.split("\n")
    # Detect this file's recurring page chrome statistically (no hard-
    # coded publisher patterns — agnostic to IEEE/AMD/RFC/etc.).
    chrome_norms = _detect_page_chrome(lines)

    out: list[str] = []
    n = len(lines)
    i = 0

    def _emit_consolidated_code(code_lines: list[str]) -> None:
        out.append("```asn.1")
        for ln in code_lines:
            # Inside a code fence, markdown escaping is unwanted (the
            # content renders literally) and embedded HTML <br> tags
            # should become real newlines.
            cleaned = re.sub(r"<br\s*/?>", "\n", ln)
            # Unescape markdown character-escapes: \*, \-, \_, \[, \],
            # \(, \), \{, \}, \<, \>, \|, \`. The backslash here is from
            # markdown safety-escaping, not part of the source content.
            cleaned = re.sub(r"\\([*\-_\[\](){}<>|`])", r"\1", cleaned)
            out.append(cleaned)
        out.append("```")

    while i < n:
        line = lines[i]
        if line.strip() != "```asn.1":
            out.append(line)
            i += 1
            continue

        # We're at a code fence. Collect its body to inspect for the
        # BEGIN scope-anchor.
        body_start = i + 1
        body_end = body_start
        while body_end < n and lines[body_end].strip() != "```":
            body_end += 1
        body = "\n".join(lines[body_start:body_end])
        is_module_scope = bool(re.search(r"\bDEFINITIONS\s*::=\s*BEGIN\b", body))

        if is_module_scope:
            # Aggressive consolidation: absorb everything until next
            # markdown heading (clause boundary).
            code_buf: list[str] = lines[body_start:body_end]
            j = body_end + 1  # past closing ```
            while j < n:
                ln = lines[j]
                s = ln.strip()
                if _MD_HEADING.match(ln):
                    break  # clause boundary — stop
                if not s:
                    j += 1
                    continue
                if _is_page_chrome(ln, chrome_norms):
                    j += 1
                    continue
                if s == "```asn.1" or s == "```":
                    j += 1
                    continue  # fence marker, drop
                code_buf.append(ln)
                j += 1
            _emit_consolidated_code(code_buf)
            i = j
            continue

        # Non-module fence: chrome-only merge with adjacent fences
        code_buf = lines[body_start:body_end]
        i = body_end + 1
        while True:
            j = i
            absorbed: list[str] = []
            while j < n:
                ln = lines[j]
                if not ln.strip():
                    j += 1
                    continue
                if _is_page_chrome(ln, chrome_norms):
                    j += 1
                    continue
                if _is_code_continuation_orphan(ln):
                    absorbed.append(ln)
                    j += 1
                    continue
                break
            if j < n and lines[j].strip() == "```asn.1":
                code_buf.extend(absorbed)
                j += 1
                while j < n and lines[j].strip() != "```":
                    code_buf.append(lines[j])
                    j += 1
                j += 1
                i = j
                continue
            break
        _emit_consolidated_code(code_buf)

    return "\n".join(out)


def _wrap_code_blocks(text: str) -> str:
    """If a text block contains ASN.1/SMI MIB markers, code-fence it.
    For first-page blocks that start with prose intro followed by a
    module-declaration, split the prose off as a separate paragraph.
    For continuation-page blocks (MIB content without `DEFINITIONS BEGIN`
    or major macros), wrap the whole block as code — Parse splits MIB
    modules across multiple JSON blocks at page boundaries, so the bulk
    of the content lives in continuation blocks lacking the strong
    module-start anchor."""
    if not _ASN1_MARKERS.search(text):
        return text
    # Try to find a clean prose-vs-code split point (module declaration
    # or a major SMIv2 macro at the start of code).
    split_anchor = re.search(
        r"\b[A-Z][A-Z0-9-]*-MIB\s+DEFINITIONS\s*::=\s*BEGIN\b"
        r"|\b(?:MODULE-IDENTITY|TEXTUAL-CONVENTION)\b\s+\w",
        text,
    )
    if split_anchor:
        prose = text[:split_anchor.start()].rstrip()
        code  = text[split_anchor.start():].strip()
        out: list[str] = []
        if prose:
            out.append(prose)
            out.append("")
        out.append("```asn.1")
        out.append(_reformat_asn1(code))
        out.append("```")
        return "\n".join(out)
    # Continuation block — no clean split point; treat the whole block
    # as MIB code (Parse-split page-2+ content of a module).
    return f"```asn.1\n{_reformat_asn1(text)}\n```"


def _strip_orphan_latex(text: str) -> str:
    """Remove LaTeX formatting commands and orphan tabular preambles
    Parse emits inline. Keep the underlying content text."""
    text = _ORPHAN_TABULAR_PREAMBLE.sub("", text)
    text = _LATEX_MULTI.sub(r"\1", text)
    text = _LATEX_MULTI_MALFORMED.sub("", text)
    return text


# Convert em-dash-separated bullet sequences into GFM tables when they
# follow a `**Table N--...**` caption. Parse encodes spec tables as
# multiline em-dash-delimited content that survives the LaTeX-cleanup
# pass as a bullet list; this post-processor rebuilds the tabular form.
_TABLE_CAPTION = re.compile(r"^\s*\*?\*?Table\s+[\w.\-]+", re.IGNORECASE)
_EM_DASH_SPLIT = re.compile(r"\s+—\s+")
# How many lines after a Table caption we'll scan for an eligible
# bullet block. Spec captions often have an extra paragraph between the
# caption and the table rows.
_TABLE_ZONE = 40


def _bullet_to_cells(bullet_line: str) -> list[str]:
    """Strip the `- ` bullet marker and split on em-dash with surrounding
    whitespace. Returns the cells with empty ones trimmed off the ends."""
    s = bullet_line.lstrip()
    if s.startswith("- "):
        s = s[2:]
    cells = [c.strip() for c in _EM_DASH_SPLIT.split(s)]
    while cells and not cells[0]:
        cells.pop(0)
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _bullets_to_gfm_table(bullet_lines: list[str]) -> str | None:
    """Render a bullet block as one or more GFM tables. Requires the
    modal column count to be ≥2 and to dominate the rows (≥50%). Uses
    max(modal, header) as the actual table width so a 3-column header
    isn't truncated when most data rows have 2 cells (Parse's
    \\multirow stripping leaves data rows shorter than the header).

    Single-cell rows in the middle of a multi-column block are treated
    as logical section dividers (almost always originated from
    \\multicolumn cells that span the full width to partition the table
    into independent sub-tables). The block is split at those rows; each
    sub-table is emitted with its own header row + the divider's content
    as a bold heading above it. GFM doesn't support cell-spanning, so
    this is the closest faithful rendering.

    Returns None if the shape isn't tabular."""
    rows = [_bullet_to_cells(b) for b in bullet_lines]
    counts = Counter(len(r) for r in rows if len(r) >= 2)
    if not counts:
        return None
    modal, freq = counts.most_common(1)[0]
    if modal < 2 or freq < 2 or freq / max(1, len(rows)) < 0.5:
        return None

    # Header is authoritative for column count; data rows pad if shorter.
    header_count = len(rows[0])
    ncols = max(modal, header_count)

    # Split data rows into sub-tables at single-cell rows (multicolumn
    # section dividers). The very first row stays as the header for all
    # sub-tables (we re-emit it before each).
    header_row = rows[0]
    data_rows = rows[1:]
    sub_tables: list[tuple[str | None, list[list[str]]]] = []
    current_heading: str | None = None
    current_rows: list[list[str]] = []
    for r in data_rows:
        if len(r) == 1 and r[0]:
            # Section divider — flush current, start new
            if current_rows or current_heading is not None:
                sub_tables.append((current_heading, current_rows))
            current_heading = r[0]
            current_rows = []
        else:
            current_rows.append(r)
    if current_rows or current_heading is not None:
        sub_tables.append((current_heading, current_rows))

    def _fmt_row(cells: list[str]) -> str:
        padded = (cells + [""] * ncols)[:ncols]
        return "| " + " | ".join(_escape_table_cell(c) for c in padded) + " |"

    separator = "|" + "|".join(["---"] * ncols) + "|"
    parts: list[str] = []
    for idx, (heading, sub_rows) in enumerate(sub_tables):
        if heading is not None:
            if idx > 0:
                parts.append("")  # blank line between sub-tables
            parts.append(f"**{_escape_table_cell(heading)}**")
            parts.append("")
        if not sub_rows:
            continue
        parts.append(_fmt_row(header_row))
        parts.append(separator)
        parts.extend(_fmt_row(r) for r in sub_rows)

    return "\n".join(parts).strip()


def _detect_and_render_tables(text: str) -> str:
    """Scan for `**Table N--...**` captions; if a bullet block within the
    following _TABLE_ZONE lines parses as a GFM table, replace it with
    the rendered table. Otherwise leave the bullets in place."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    table_zone = 0  # lines remaining where we'll look for a bullet block
    while i < len(lines):
        line = lines[i]
        if _TABLE_CAPTION.match(line):
            table_zone = _TABLE_ZONE
            out.append(line)
            i += 1
            continue

        if table_zone > 0 and line.startswith("- "):
            # Collect contiguous bullet block (allowing blank-line gaps of
            # length 1 inside)
            block_start = i
            while i < len(lines):
                if lines[i].startswith("- "):
                    i += 1
                    continue
                # allow a single blank line inside the block
                if i + 1 < len(lines) and not lines[i].strip() and lines[i + 1].startswith("- "):
                    i += 1
                    continue
                break
            block = [b for b in lines[block_start:i] if b.startswith("- ")]
            if len(block) >= 2:
                rendered = _bullets_to_gfm_table(block)
                if rendered:
                    out.append("")
                    out.append(rendered)
                    out.append("")
                    table_zone = 0
                    continue
            # No table: emit as-is
            out.extend(lines[block_start:i])
            table_zone = 0
            continue

        if table_zone > 0:
            table_zone -= 1
        out.append(line)
        i += 1

    return "\n".join(out)


def _split_latex_rows(body: str) -> list[list[str]]:
    """LaTeX rows are separated by '\\' or '\'. Cells within a row by '&'.
    Strip trailing-empty cells from each row (LaTeX rows often end with a
    hanging '&')."""
    # Normalise: turn the row terminator into a single sentinel before splitting.
    body = re.sub(r"\\\\", "|||ROW|||", body)        # `\\` -> sentinel
    body = re.sub(r"(?<![A-Za-z_])\\(?![A-Za-z_])",  # bare `\` not part of a word
                  "|||ROW|||", body)
    raw_rows = body.split("|||ROW|||")
    rows: list[list[str]] = []
    for r in raw_rows:
        if not r.strip():
            continue
        cells = [c.strip() for c in r.split("&")]
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            rows.append(cells)
    return rows


def _row_is_decorative(cells: list[str]) -> bool:
    return all(_DECOR_CELL.match(c) for c in cells)


def _escape_table_cell(s: str) -> str:
    """GFM-escape: literal `|` inside a cell would close the row; collapse
    any embedded newlines to spaces (GFM cells are single-line)."""
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _rows_to_gfm_or_bullets(rows: list[list[str]]) -> str:
    """Shared helper used by both _render_latex_tabular and the caption-
    driven bullet reconstruction: given parsed rows, render as a GFM
    table when the shape is dominantly tabular (≥ 50% rows match modal
    column count, modal ≥ 2). Single-cell rows in the middle become
    sub-table dividers (bold heading + repeat of the column header for
    each sub-section). Falls back to a bullet list when no tabular
    structure dominates."""
    rows = [r for r in rows if not _row_is_decorative(r)]
    if not rows:
        return ""
    counts = Counter(len(r) for r in rows if len(r) >= 2)
    modal, freq = counts.most_common(1)[0] if counts else (0, 0)

    if modal >= 2 and freq >= 2 and freq / max(1, len(rows)) >= 0.5:
        # Tabular — emit as GFM table with sub-table partitioning at
        # single-cell rows (multicolumn dividers in the source).
        header_count = len(rows[0])
        ncols = max(modal, header_count)
        header_row = rows[0]
        data_rows = rows[1:]
        sub_tables: list[tuple[str | None, list[list[str]]]] = []
        current_heading: str | None = None
        current_rows: list[list[str]] = []
        for r in data_rows:
            if len(r) == 1 and r[0]:
                if current_rows or current_heading is not None:
                    sub_tables.append((current_heading, current_rows))
                current_heading = r[0]
                current_rows = []
            else:
                current_rows.append(r)
        if current_rows or current_heading is not None:
            sub_tables.append((current_heading, current_rows))

        def _fmt_row(cells: list[str]) -> str:
            padded = (cells + [""] * ncols)[:ncols]
            return "| " + " | ".join(_escape_table_cell(c) for c in padded) + " |"

        separator = "|" + "|".join(["---"] * ncols) + "|"
        parts: list[str] = []
        for idx, (heading, sub_rows) in enumerate(sub_tables):
            if heading is not None:
                if idx > 0:
                    parts.append("")
                parts.append(f"**{_escape_table_cell(heading)}**")
                parts.append("")
            if not sub_rows:
                continue
            parts.append(_fmt_row(header_row))
            parts.append(separator)
            parts.extend(_fmt_row(r) for r in sub_rows)
        return "\n".join(parts).strip()

    # Otherwise: bullet list joining cells with " — ".
    lines: list[str] = []
    for r in rows:
        nonempty = [c for c in r if c]
        if not nonempty:
            continue
        joined = " — ".join(_escape_table_cell(c).replace("\\|", "|") for c in nonempty)
        lines.append(f"- {joined}")
    return "\n".join(lines)


def _render_latex_tabular(body: str) -> str:
    """Decide the best markdown rendering for one LaTeX-tabular body."""
    return _rows_to_gfm_or_bullets(_split_latex_rows(body))


def _process_latex_blocks(text: str) -> str:
    """Replace every `\\begin ... \\end` Parse fragment with the best
    available markdown rendering. Falls back to a fenced code block if the
    body can't be parsed as a sensible row/cell grid."""
    def replace(match: re.Match) -> str:
        body = match.group(1)
        try:
            rendered = _render_latex_tabular(body)
        except Exception:
            rendered = ""
        if rendered:
            return "\n" + rendered + "\n"
        # Fallback: preserve raw form in a fenced block so it's at least visible.
        return f"\n```latex\n\\begin{body}\\end\n```\n"
    return _LATEX_TABULAR.sub(replace, text)


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

    # Track the resolved section field for context, but DO NOT emit a
    # `## §<section>` anchor here. _apply_toc() resolves `section` by
    # page number (`bisect_right` against TOC start_pages), so when a
    # clause starts mid-page, every block on that page — including the
    # earlier clauses' headings — gets tagged with the LATER clause.
    # Emitting on a section-field transition then puts the section
    # marker visually before content that doesn't belong to it. Instead,
    # heading blocks emit at clause-derived levels (see below), which
    # IS position-accurate because Parse / PyMuPDF4LLM detect headings
    # at their actual on-page positions.
    if section and section != state.get("section"):
        state["section"] = section

    # Convert any Parse-emitted LaTeX-tabular fragments into GFM tables /
    # bullet lists / code-fenced fallback before further block-type handling.
    if "\\begin" in text:
        text = _process_latex_blocks(text)
    # Strip orphan LaTeX commands (Parse emits {tabular}{ccc}, \multicolumn,
    # \multirow inline for some table types without the \begin/\end wrapper).
    if "{tabular}" in text or "\\multicolumn" in text or "\\multirow" in text:
        text = _strip_orphan_latex(text)

    if btype == "heading":
        # On-page heading text. _md_page_to_blocks() stripped the leading
        # `#`s, so re-add at a level derived from the clause-number depth
        # in the heading text (`11.4` → ##; `11.3.4` → ###; `12.29.1.1`
        # → ####). This makes outline-panel hierarchy track the actual
        # clause hierarchy in the document, independent of the unreliable
        # page-granular `section` field.
        level = _heading_level(text)
        lines.append("")
        lines.append("#" * level + f" {text}")
        lines.append("")
    elif btype == "table":
        # text is already a markdown table — preserved whole by
        # _md_page_to_blocks(). Pass through.
        lines.append(text)
        lines.append("")
    else:
        # Paragraph. text already carries inline markdown (bold/sup/<br>) so
        # the preview renders it directly. If the block looks like ASN.1 /
        # SMIv2 MIB source code (Parse extracts MIB definitions as flowed
        # text), wrap as a fenced code block with line breaks at
        # statement boundaries.
        text = _wrap_code_blocks(text)
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
    md = "\n".join(out).rstrip() + "\n"

    # Final pass: rebuild tabular structure from bullet sequences that
    # follow `**Table N--...**` captions (Parse-emitted spec tables that
    # survived the LaTeX-cleanup pass as plain em-dash-separated bullets).
    md = _detect_and_render_tables(md)
    # Merge multi-block MIB-code fences split by page-chrome interruptions.
    md = _consolidate_code_fences(md)
    # Collapse long runs of identical characters (Parse-extracted ASCII-art
    # divider lines) so the preview doesn't horizontal-scroll forever.
    md = _collapse_long_runs(md)
    return md


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
