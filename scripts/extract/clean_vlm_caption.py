#!/usr/bin/env python3
"""
Meta-stripper for VLM captions (Phase F).

Removes framing leak from VLM captions so the cleaned string is suitable
for direct insertion as a RAG chunk. Pure stdlib regex pipeline,
idempotent, side-effect-free.

The stripper targets the failure modes observed in the 2026-05-25
Phase F smoke (14 images across 5 diagram types, 5 VLMs tested):

  1. Thinking-model `<think>...</think>` blocks (MiniCPM-V family).
  2. Runaway repetition loops (MiniCPM regenerating the same paragraph
     100+ times before hitting num_predict). Truncate after N repeats.
  3. Intro sentences ("Based on the diagram, the following...",
     "The diagram asserts:", "### Key Components", "Final Answer:").
  4. Closer paragraphs ("This summary aligns with...", "These facts
     are derived from...", "For more information consult...").
  5. Markdown structure leak (#### headers, **Bold Headings:**, ---).
  6. LaTeX wrapper leak (`\\boxed{X}` → `X`, `$$X$$` → `X`).
  7. Trailing whitespace + multi-blank-line collapse.

NOT handled (intentionally — too risky for v1):
  - Hallucinated acronym expansions like "STA (System Test Agent)"
    — some parentheticals are legitimate ("(optional)", "(in octets)")
    so a blanket strip would lose real content. Defer until we have
    a per-acronym dictionary.
  - Visual-description verbs ("positioned above", "via a directional
    arrow") inside otherwise-good propositions. Requires sentence-level
    rewrite, not regex.

Public API:
    strip(raw: str) -> str          # the main stripper
    is_likely_caption(raw: str)     # heuristic: did the model output
                                    # something usable, or pure junk?

Hand-rolled fixtures from the smoke runs live in
`test_clean_vlm_caption.py`. Re-strip before-and-after on real captures
is the validation gate, not synthetic examples.
"""

from __future__ import annotations

import re


# -- Pass 1: <think>...</think> blocks --------------------------------
# MiniCPM-V outputs reasoning traces before the final answer; we want
# only the final answer.
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", flags=re.DOTALL | re.IGNORECASE)


def _strip_think_blocks(text: str) -> str:
    return _THINK_BLOCK.sub("", text)


# -- Pass 2: runaway repetition ---------------------------------------
# MiniCPM hit a generation loop where the same sentence/paragraph
# regenerates 100+ times. Detect a normalized line that recurs more than
# N times within the output and truncate at the (N+1)th occurrence.
#
# We normalize by collapsing whitespace and lowercasing before counting,
# so near-identical lines (e.g. trailing punctuation drift) still cluster.
_REPETITION_THRESHOLD = 3


def _truncate_runaway_repetition(text: str) -> str:
    lines = text.split("\n")
    counts: dict[str, int] = {}
    cut_at: int | None = None
    for i, line in enumerate(lines):
        key = re.sub(r"\s+", " ", line.strip().lower())
        if len(key) < 20:  # short lines (blank, single bullets) don't trigger
            continue
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > _REPETITION_THRESHOLD:
            cut_at = i
            break
    if cut_at is None:
        return text
    return "\n".join(lines[:cut_at]).rstrip()


# -- Pass 3: intro sentences ------------------------------------------
# Match against the FIRST non-blank lines; strip while they match.
# Patterns are derived from concrete observed openings in the 14-image
# smoke set, not speculation. New patterns get added as we discover them.
_INTRO_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        # Direct meta-commentary openers — allow extra qualifiers between
        # the noun and the `,`/`:` (e.g. "Based on the provided image
        # description, ..." — `image` is followed by `description` then `,`).
        r"^based on the (provided )?(diagram|description|image|figure|"
        r"reference)[^,:]{0,40}[,:]",
        r"^the (diagram|figure|image) (asserts|describes|includes|contains|"
        r"depicts|shows|explicitly asserts|is asserting|specifies)",
        r"^the diagram is a (representation|schematic|block diagram|"
        r"diagrammatic representation)",
        r"^the given (image|diagram|figure) describes",
        r"^this (diagram|figure|image) (asserts|describes|depicts|shows)",
        r"^here (is|are) (a |the )?(list|summary|breakdown|description)",
        r"^the following (is|are) (a |the )?",
        r"^to address this task",
        r"^the diagram explicitly asserts",
        # v2-ctx analytical-prose intros observed 2026-05-26 on
        # qwen3-vl:8b: model wraps its output in summary framing despite
        # the prompt's "no introduction" instruction.
        r"^below (is|are) (a |the )?(concise |brief |short |detailed )?"
        r"(summary|breakdown|list|description|analysis)",
        r"^(here|below) (we|i) (summarise|present|describe|outline)",
        r"^(key |the )?(facts?|points?|findings?|details?|takeaways?|"
        r"observations?|notes?|highlights?|relationships?)( derived | extracted | "
        r"from)?",
        r"^the (diagram|figure|image) (communicates|provides|illustrates|"
        r"shows|presents|conveys) the following",
        r"^the (diagram|figure|image) is asserting",
        r"^the diagram in this section",
        # Section-style headers used as preambles
        r"^#{1,4}\s",  # markdown h1-h4
        r"^\*\*[a-z][^*]{0,80}\*\*:?\s*$",  # bold standalone heading line
        r"^(key )?(assertions|components|signal|signals|relationships|"
        r"connections|summary|takeaways|takeaway|facts|findings|"
        r"final answer|notes)[\s:]*$",
        # Math-problem framing
        r"^final answer[:\s]",
        r"^\\boxed\{",
        # Thinking-model preamble that escaped <think>. Deliberately
        # narrow — "first|firstly|first of all" would also match
        # propositions that legitimately enumerate ("First fact: ...").
        # Only match when followed by explicit reasoning constructs.
        r"^(okay|alright)[,\s]",
        r"^(let me|i need to|i think|i'll|i am going to|i should)\s",
        r"^(to (begin|start)|firstly,|first of all,)[,\s]",
    ]
]


def _is_preamble_shape(line: str) -> bool:
    """True if the line looks like a pure preamble with no embedded
    content. Preambles end with `:` (or `:.`), start with markdown
    headers, or are bold-only headings — they carry no facts on their
    own and are always safe to strip when matched by an intro/closer
    pattern.

    Lines NOT of preamble shape (e.g. "The diagram asserts that there
    are two LLDP Agents.") embed real content in their intro framing
    and need the content-survival check before they can be dropped."""
    line = line.strip()
    if line.endswith(":") or line.endswith(":."):
        return True
    if line.startswith("#"):
        return True
    if line.startswith("**") and (line.endswith("**") or line.endswith("**:")):
        return True
    return False


def _strip_leading_intro(text: str) -> str:
    """Strip leading lines that match intro patterns. Preamble-shaped
    lines (ending `:`, markdown headers, bold-only) are always stripped.
    Sentence-shaped lines that match an intro pattern are only stripped
    if real content survives after them — otherwise the model's intro
    framing IS the content and dropping it would delete the caption."""
    lines = text.split("\n")
    n = len(lines)
    keep_from = 0
    while keep_from < n:
        stripped = lines[keep_from].strip()
        if not stripped:
            keep_from += 1
            continue
        if not any(p.match(stripped) for p in _INTRO_PATTERNS):
            break
        if not _is_preamble_shape(stripped):
            has_content_after = any(
                lines[j].strip() and not any(p.match(lines[j].strip())
                                             for p in _INTRO_PATTERNS)
                for j in range(keep_from + 1, n)
            )
            if not has_content_after:
                break
        keep_from += 1
    return "\n".join(lines[keep_from:])


# -- Pass 4: closer paragraphs ----------------------------------------
# Closing sentences/paragraphs at the END of output. We strip while the
# last non-blank LINE matches (closers are usually a single sentence,
# sometimes a 2-3 line paragraph; we strip line-by-line from the tail).
_CLOSER_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^this (summary|analysis|interpretation|description|setup|"
        r"sequence|breakdown) (accurately |aligns |reflects |captures |"
        r"is |provides |represents |describes |shows |conveys )",
        r"^this (interpretation|analysis|description) is derived from",
        r"^this (is typical for|setup is typical|aligns with)",
        r"^these (facts|assertions|relationships|propositions) "
        r"(describe|are derived|reflect|aligned|describe the|provide)",
        r"^the diagram does not (require|provide|specify) (referencing|"
        r"specific|exact|additional|further)",
        r"^for (precise|more|further|additional) [a-z]+ "
        r"(details|information|clarification|context|reference|guidance)",
        r"^for (precise|more|further|additional) "
        r"(details|information|clarification|context|reference|guidance)",
        r"^all signals are low outside the specific intervals defined",
        # Suppression-list-leak closers — the model echoes back the
        # prompt's "do not describe visual annotations" instruction as
        # if it were a finding. Observed 2026-05-25 on qwen3-vl:8b v2.
        r"^no (additional |further )?(visual )?annotations? "
        r"(\(.*?\) )?(are|were) (interpreted|described|referenced|provided)",
        r"^the focus remains (strictly )?on (the )?(explicitly )?",
        r"^only the (explicitly )?(stated|labeled) (elements|components|"
        r"labels|annotations)",
        # math-problem closers
        r"^final answer[:\s]",
        r"^\\boxed\{",
        # markdown horizontal rules / structural markers
        r"^-{3,}\s*$",
        r"^={3,}\s*$",
        r"^\*{3,}\s*$",
        # bold-heading closer
        r"^\*\*[a-z][^*]{0,80}\*\*:?\s*$",
        r"^#{1,4}\s",
        # heuristic: trailing single-word section markers
        r"^(summary|conclusion|note|notes)\s*[:.]?\s*$",
    ]
]


def _strip_trailing_closer(text: str) -> str:
    """Mirror of the intro pass for the tail: preamble-shape (markdown
    headers, ---/===/***, bold-only) always strips; sentence-shape only
    strips if real content survives before it."""
    lines = text.split("\n")
    n = len(lines)
    keep_until = n
    while keep_until > 0:
        i = keep_until - 1
        stripped = lines[i].strip()
        if not stripped:
            keep_until = i
            continue
        if not any(p.match(stripped) for p in _CLOSER_PATTERNS):
            break
        if not _is_preamble_shape(stripped):
            has_content_before = any(
                lines[j].strip() and not any(p.match(lines[j].strip())
                                             for p in _CLOSER_PATTERNS)
                for j in range(i)
            )
            if not has_content_before:
                break
        keep_until = i
    return "\n".join(lines[:keep_until])


# -- Pass 5: per-line markdown / LaTeX leak inside body ---------------
# Applied per-line so we don't lose propositions that share a line with
# the markdown noise. Order matters: unwrap LaTeX first, then strip
# leading markup.

# `\boxed{x}` → `x`; we keep the content because sometimes it carries a
# real value (e.g. `\boxed{507}` for max packet length).
_BOXED = re.compile(r"\\boxed\{([^{}]*)\}")
# `$$...$$` and `$...$` with no actual LaTeX operators — strip wrapper.
_DOLLAR_MATH_BLOCK = re.compile(r"\$\$([^$]*)\$\$")


def _unwrap_latex(text: str) -> str:
    text = _BOXED.sub(r"\1", text)
    text = _DOLLAR_MATH_BLOCK.sub(r"\1", text)
    return text


# Per-line: strip leading markdown headers + bold-only-line "headings"
# that survived passes 3 and 4 (e.g. middle-of-body section breaks).
_INLINE_HEADER = re.compile(r"^\s*#{1,4}\s+")
_BOLD_ONLY = re.compile(r"^\s*\*\*([^*]+)\*\*:?\s*$")
_HR_LINE = re.compile(r"^\s*(-{3,}|={3,}|\*{3,})\s*$")

# Numbered+bold section headers ("1. **Title**:", "- **Heading**:")
# leak from the v2-ctx prompt on dense diagrams. Strip ONLY when the
# line is entirely a header — bold identifiers inside a proposition
# ("1. **MMCM** has CLKIN1 input connected to **IBUFD_S**.") must be
# preserved. End-of-line anchor distinguishes the two cases.
_NUMBERED_BOLD_HEADER = re.compile(
    r"^\s*(?:\d+\.\s+|[-*+]\s+)\*\*([^*]+)\*\*\s*:?\s*$"
)


def _strip_inline_markdown(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if _HR_LINE.match(line):
            continue  # horizontal rules → drop
        line = _INLINE_HEADER.sub("", line)  # `#### X` → `X`
        # `1. **Header Title**:` standalone (whole line) → drop. The
        # regex's $ anchor guarantees there's no body sentence; a line
        # like "1. **MMCM** has CLKIN1 input" doesn't match and is
        # preserved with its bold span intact.
        if _NUMBERED_BOLD_HEADER.match(line):
            continue
        bold_match = _BOLD_ONLY.match(line)
        if bold_match:
            # `**Heading:**` standalone → drop or unwrap
            text_only = bold_match.group(1).strip().rstrip(":")
            # Drop short bold-only lines (headings); keep if substantive
            if len(text_only) < 60 and " " not in text_only.strip():
                continue
            line = text_only
        out.append(line)
    return "\n".join(out)


# -- Pass 6: whitespace normalization ---------------------------------
# Collapse runs of blank lines (caused by stripped intros/closers
# leaving gaps); trim trailing whitespace per line; trim overall.
_MULTI_BLANK = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")


def _normalize_whitespace(text: str) -> str:
    text = _TRAILING_WS.sub("\n", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


# -- Public API -------------------------------------------------------

def strip(raw: str) -> str:
    """Apply the full stripping pipeline. Idempotent.

    Order of passes matters: thinking blocks first (they contain things
    that LOOK like intros/closers); then truncate runaway repetition
    before pattern matching (so we don't waste time on the loop body);
    then intros, closers, per-line markdown, and finally whitespace.
    """
    if not raw or not raw.strip():
        return ""
    text = raw
    text = _strip_think_blocks(text)
    text = _truncate_runaway_repetition(text)
    text = _strip_leading_intro(text)
    text = _strip_trailing_closer(text)
    text = _unwrap_latex(text)
    text = _strip_inline_markdown(text)
    text = _normalize_whitespace(text)
    return text


def is_likely_caption(raw: str, min_chars: int = 40) -> bool:
    """Heuristic: did the model produce something usable, or pure junk?

    Returns False for empty output, output that's just whitespace, or
    output that's too short to be a real caption (likely a refusal or
    a single-token glitch). Use as a cheap gate before / after stripping
    to decide whether to fall back, retry, or drop the picture block.
    """
    if not raw:
        return False
    cleaned = strip(raw)
    return len(cleaned) >= min_chars


if __name__ == "__main__":
    # CLI mode: read raw caption from stdin, write stripped to stdout.
    # Useful for one-off testing: `echo "..." | python clean_vlm_caption.py`
    import sys
    print(strip(sys.stdin.read()))
