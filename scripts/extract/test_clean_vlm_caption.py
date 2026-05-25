#!/usr/bin/env python3
"""
Tests for `clean_vlm_caption.strip()` using REAL captured outputs from
the 2026-05-25 Phase F VLM smoke runs (14 images, 4 PDFs, 5 VLMs).

Fixtures here are excerpts of actual raw outputs — not synthetic
examples — so the tests double as regression evidence that the
stripper handles the failure modes we observed in practice.

Run with: python test_clean_vlm_caption.py
(no pytest dependency — pure unittest, stdlib only.)
"""

from __future__ import annotations

import unittest

from clean_vlm_caption import strip, is_likely_caption


class TestThinkBlocks(unittest.TestCase):
    """MiniCPM-V emits <think>...</think> reasoning before the answer.
    We want only the answer."""

    def test_strips_simple_think_block(self):
        raw = "<think>\nLet me analyse this.\n</think>\n1. Foo bar.\n2. Baz qux."
        out = strip(raw)
        self.assertNotIn("<think>", out)
        self.assertNotIn("Let me analyse", out)
        self.assertIn("Foo bar", out)
        self.assertIn("Baz qux", out)

    def test_strips_multiline_think_block(self):
        # Excerpted from MiniCPM-V 4.5 image 1 (8021AB-2016 p.1 IEEE logo)
        raw = """<think>
Okay, I need to extract all the explicit facts from this IEEE standards document without mentioning any details about how it's drawn or its visual structure. Let me start by looking at what is explicitly stated.

Firstly, there is a label: "IEEE STANDARDS ASSOCIATION". That means that entity exists.
</think>

1. An entity labeled "IEEE STANDARDS ASSOCIATION" is present in the diagram.
2. Adjacent to this label are two symbols explicitly identified as "IEEE"."""
        out = strip(raw)
        self.assertNotIn("Okay, I need to", out)
        self.assertNotIn("</think>", out)
        self.assertIn("IEEE STANDARDS ASSOCIATION", out)


class TestRunawayRepetition(unittest.TestCase):
    """MiniCPM-V hit a generation loop on dense diagrams, regenerating
    the same paragraph 100+ times. Truncate at the 4th occurrence."""

    def test_truncates_repeated_paragraph(self):
        # Excerpted from MiniCPM-V 4.5 image 2 (8021AB-2016 p.27)
        # — actual output had this paragraph regenerated ~100 times.
        loop_line = (
            "Optional modules such as PTOPO MIB and others have arrows "
            "pointing towards this central transmission/reception section."
        )
        raw = "\n\n".join([
            "Some real content at the start.",
            loop_line,
            "More real content.",
            loop_line,
            loop_line,
            loop_line,
            loop_line,
            loop_line,
            loop_line,
            loop_line,
        ])
        out = strip(raw)
        self.assertIn("Some real content", out)
        # The looped line should appear at most _REPETITION_THRESHOLD times
        # in the truncated output (the cut happens at the (N+1)th occurrence)
        self.assertLessEqual(out.count(loop_line), 4)
        # The output should be much shorter than the input
        self.assertLess(len(out), len(raw) * 0.7)

    def test_preserves_non_repeated_content(self):
        raw = """First fact about the diagram.
Second fact about a component.
Third fact about a relationship.
Fourth fact about a constraint."""
        out = strip(raw)
        # No repetition → no truncation, all facts preserved
        self.assertIn("First fact", out)
        self.assertIn("Fourth fact", out)


class TestIntroStripping(unittest.TestCase):
    """Strip the leading meta-commentary openers observed in the smoke."""

    def test_strips_based_on_diagram_intro(self):
        # From qwen3-vl:8b CoT image 4 (8021AB-2016 p.32 MAC Relay)
        raw = """Based on the diagram, the following factual statements are true:

1. Two LLDP Agent components exist.
2. Each LLDP Agent connects to one LLC component."""
        out = strip(raw)
        self.assertNotIn("Based on the diagram", out)
        self.assertIn("LLDP Agent", out)

    def test_strips_diagram_asserts_intro(self):
        # From InternVL3.5-8B image 3 (8021AB-2016 p.31 multi-agent)
        raw = """The diagram asserts:

- There is an LLDp management entity.
- Multiple LLDp agents are connected to the LLDp management entity."""
        out = strip(raw)
        # The intro should be gone but the propositions should remain
        self.assertFalse(out.lstrip().startswith("The diagram asserts"))
        self.assertIn("LLDp management entity", out)

    def test_strips_markdown_header_intro(self):
        # From qwen3-vl:8b v2 image 3 (pg047 p.102 GTH transceiver)
        raw = """### Key Components and Relationships in the System

#### **1. Core Components and Signals**
- **1G/2.5G Ethernet PCS/PMA or SGMII LogiCORE**
  - Located within the `<component_name>_block`."""
        out = strip(raw)
        self.assertNotIn("### Key Components", out)
        # Note: the body still has nested ### headers that pass 5 will
        # also strip, so the bold content should survive
        self.assertIn("LogiCORE", out)

    def test_strips_final_answer_intro(self):
        # From qwen3-vl:8b v2 image 1 (8021AB-2016 p.51 TLV format)
        raw = """### Final Answer:
$$
\\boxed{507}
$$"""
        out = strip(raw)
        self.assertNotIn("Final Answer", out)
        self.assertIn("507", out)


class TestCloserStripping(unittest.TestCase):
    """Strip trailing meta-commentary observed in the smoke."""

    def test_strips_this_summary_closer(self):
        # From qwen3-vl:8b CoT image 4 (8021AB-2016 p.32)
        raw = """1. Two LLDP Agents exist.
2. Each connects to one LLC.

This summary accurately reflects the layout and relationships shown in the diagram."""
        out = strip(raw)
        self.assertNotIn("This summary accurately reflects", out)
        self.assertIn("LLDP Agents", out)

    def test_strips_suppression_list_leak_closer(self):
        # From qwen3-vl:8b v2 image 1 (pg099 timing diagram) — the prompt
        # explicitly told the model NOT to reference dashed lines / grid
        # axes; the model dutifully echoed the suppression list back in
        # its closer.
        raw = """1. mdio signal is periodic.
2. Each transition follows the clock.

This interpretation is derived from the visual representation of the signals, where the transitions and active periods align with the described relationships. The diagram does not require referencing specific visual elements like dashed lines or grid axes, as the timing constraints are directly observable in the signal traces."""
        out = strip(raw)
        self.assertNotIn("This interpretation is derived", out)
        self.assertNotIn("dashed lines or grid axes", out)
        self.assertIn("mdio signal", out)

    def test_strips_suppression_list_echo_closer(self):
        # REGRESSION: caught in end-to-end validation 2026-05-25 with
        # qwen3-vl:8b v2 on 8021AB-2016 p.32. Model echoed back the
        # prompt's suppression instructions as if they were findings.
        raw = """1. Two LLDP Agent components exist.
2. Each connects via ISS link to MAC Relay Entity.

No additional visual annotations (e.g., dashed lines, color coding, or spatial arrangements) are interpreted beyond the labeled relationships and component names provided in the diagram. The focus remains strictly on the explicitly stated elements and connections."""
        out = strip(raw)
        self.assertNotIn("No additional visual annotations", out)
        self.assertNotIn("The focus remains strictly", out)
        self.assertIn("LLDP Agent", out)
        self.assertIn("MAC Relay Entity", out)

    def test_strips_for_more_information_closer(self):
        # From qwen3-vl:8b CoT image 2 (pg047 p.52 MDIO read)
        raw = """1. STA drives MDIO during the first phase.
2. Addressed MMD drives MDIO during the second phase.

For precise implementation details, consult the diagram's source documentation."""
        out = strip(raw)
        self.assertNotIn("For precise implementation details", out)
        self.assertIn("STA drives MDIO", out)


class TestLatexUnwrapping(unittest.TestCase):
    """Strip LaTeX wrappers but keep the wrapped content."""

    def test_unwraps_boxed(self):
        raw = "The maximum length is \\boxed{507} octets."
        out = strip(raw)
        self.assertNotIn("\\boxed", out)
        self.assertIn("507", out)

    def test_unwraps_double_dollar_math(self):
        raw = "The constraint is $$0 \\leq n \\leq 507$$ as specified."
        out = strip(raw)
        self.assertNotIn("$$", out)
        self.assertIn("507", out)

    def test_strips_lone_boxed_line(self):
        # From qwen3-vl:8b CoT image 3 (pg047 p.102 GTH transceiver) —
        # the model wrapped its conclusion in \boxed{} as if answering
        # a math word problem.
        raw = """The GTH core processes signals like RXDATA[15:0] and TXDATA[15:0].

\\boxed{Gigabit Transceiver}"""
        out = strip(raw)
        self.assertNotIn("\\boxed", out)
        # Either kept as "Gigabit Transceiver" or absent — both acceptable;
        # what we MUST avoid is leaving "\boxed{...}" raw in the output.
        self.assertIn("GTH core", out)


class TestInlineMarkdownStripping(unittest.TestCase):
    """Strip per-line markdown noise that survives intro/closer passes."""

    def test_strips_h3_headers_in_body(self):
        raw = """1. First proposition.

### Section Break

2. Second proposition."""
        out = strip(raw)
        self.assertNotIn("### Section Break", out)
        self.assertIn("First proposition", out)
        self.assertIn("Second proposition", out)

    def test_strips_bold_heading_lines(self):
        raw = """1. First proposition.

**Subsection:**

2. Second proposition."""
        out = strip(raw)
        # The standalone bold line should be dropped (it's a header)
        self.assertNotIn("**Subsection:**", out)
        self.assertIn("First proposition", out)

    def test_strips_horizontal_rules(self):
        raw = """1. First.
---
2. Second.
***
3. Third."""
        out = strip(raw)
        # HR lines gone, content kept
        self.assertNotIn("---", out)
        self.assertNotIn("***", out)
        self.assertIn("First", out)
        self.assertIn("Third", out)

    def test_preserves_bold_inside_proposition(self):
        # Bold spans inside a proposition line should be preserved
        # (we only drop bold-only lines, not inline bold).
        raw = "1. **MMCM** has CLKIN1 input connected to **IBUFD_S**."
        out = strip(raw)
        self.assertIn("MMCM", out)
        self.assertIn("IBUFD_S", out)


class TestWhitespaceNormalization(unittest.TestCase):
    def test_collapses_multiple_blank_lines(self):
        raw = "Line 1.\n\n\n\n\nLine 2."
        out = strip(raw)
        # At most ONE blank line between propositions
        self.assertNotIn("\n\n\n", out)

    def test_strips_trailing_whitespace(self):
        raw = "Line with trailing.   \nNext line."
        out = strip(raw)
        self.assertNotIn("   \n", out)

    def test_trims_overall(self):
        raw = "\n\n\nReal content.\n\n\n"
        out = strip(raw)
        self.assertEqual(out, "Real content.")


class TestIdempotence(unittest.TestCase):
    """strip(strip(x)) == strip(x). Critical for safety: applying the
    stripper twice (e.g. once in caption-images.py, once as defense in
    rag-server) must not damage already-stripped content."""

    def _check_idempotent(self, raw: str):
        once = strip(raw)
        twice = strip(once)
        self.assertEqual(once, twice,
                         f"strip not idempotent:\n  once:  {once!r}\n  twice: {twice!r}")

    def test_simple_propositions(self):
        self._check_idempotent("1. First.\n2. Second.\n3. Third.")

    def test_with_intro(self):
        self._check_idempotent(
            "Based on the diagram, the following facts:\n\n1. Foo.\n2. Bar."
        )

    def test_with_closer(self):
        self._check_idempotent(
            "1. Foo.\n2. Bar.\n\nThis summary describes the structure."
        )

    def test_with_think_block(self):
        self._check_idempotent(
            "<think>reasoning</think>\n\n1. Foo.\n2. Bar."
        )

    def test_empty(self):
        self._check_idempotent("")
        self._check_idempotent("   \n\n  ")

    def test_realistic_qwen3vl_output(self):
        # Lightly redacted from qwen3-vl:8b v2 image 3 (8021AB-2016 p.31)
        self._check_idempotent("""The diagram includes an entity named "LLDP management entity".
The diagram includes multiple entities named "LLDP agent".
Each "LLDP agent" entity contains an entity named "LLC".
"LLDP" is defined as "Link Layer Discovery Protocol".
"LSAP" is defined as "Link service access point".
"MSAP" is defined as "MAC service access point".""")


class TestEdgeCases(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(strip(""), "")
        self.assertEqual(strip("   "), "")
        self.assertEqual(strip("\n\n\n"), "")

    def test_only_intro(self):
        raw = "Based on the diagram, the following:"
        out = strip(raw)
        # If the entire output is intro, stripper returns empty.
        # is_likely_caption() should catch this as junk.
        self.assertEqual(out, "")
        self.assertFalse(is_likely_caption(raw))

    def test_only_think_block(self):
        raw = "<think>just reasoning, no answer</think>"
        out = strip(raw)
        self.assertEqual(out, "")

    def test_pure_junk_short(self):
        self.assertFalse(is_likely_caption(""))
        self.assertFalse(is_likely_caption("ok."))
        self.assertFalse(is_likely_caption("<think>x</think>"))

    def test_real_caption_passes_likely(self):
        raw = """LLDP management entity is at the top.
LLDP agents communicate via LSAP and MSAP."""
        self.assertTrue(is_likely_caption(raw))


class TestPreservesGoodContent(unittest.TestCase):
    """Critical regression tests: the stripper must NOT eat real content
    when there's nothing wrong with the input."""

    def test_clean_state_machine_caption_unchanged(self):
        # From qwen3-vl:8b v2 image 2 (8021AB-2016 p.70 TX state machine)
        # — this was the CLEANEST output of the whole smoke. Stripper
        # must not damage it.
        raw = """portEnabled is set to FALSE.
TX_LLDP_INITIALIZE executes txInitializeLLDP() and sets txShutdownWhile to 0.
TX_IDLE is entered when adminStatus is enabledRxTx or enabledTxOnly.
In TX_IDLE, txTTL is set to min(65535, (msgTxInterval * msgTxHold) + 1).
TX_SHUTDOWN_FRAME is entered when adminStatus is disabled or enabledRxOnly.
TX_SHUTDOWN_FRAME executes mibConstrShutdownLLDPU(), txFrame(), and sets txShutdownWhile to reinitDelay.
txShutdownWhile is 0.
TX_INFO_FRAME is entered when txNow is TRUE and txCredit is greater than 0.
TX_INFO_FRAME executes mibConstrInfoLLDPU(), txFrame(), dec(txCredit), and sets txNow to FALSE.
TX_INFO_FRAME has UCT."""
        out = strip(raw)
        # All 10 propositions must survive; preserving exact text matters
        # because these are the kind of clean outputs we want untouched.
        self.assertEqual(out, raw.strip())

    def test_preserves_single_sentence_caption_with_framing(self):
        # REGRESSION: caught in end-to-end validation 2026-05-25 with
        # qwen3-vl:8b on 8021AB-2016 p.32. The whole caption was one
        # long sentence starting with "The diagram asserts that" — the
        # naive intro stripper deleted ALL of it. The fix: only strip
        # an intro-matching line if real content survives after it.
        raw = (
            "The diagram asserts that there are two LLDP Agents. Each "
            "LLDP Agent is connected to an LLC entity labeled \"LLC-ISS\". "
            "Each LLC entity labeled \"LLC-ISS\" is connected via an "
            "\"ISS\" link to a MAC Relay Entity."
        )
        out = strip(raw)
        # Content must survive even though the line starts like an intro
        self.assertIn("LLDP Agents", out)
        self.assertIn("MAC Relay Entity", out)
        self.assertGreater(len(out), 50,
                           f"stripper ate single-sentence caption: {out!r}")

    def test_preserves_single_sentence_closer_shaped_caption(self):
        # Same regression on the closer side.
        raw = "These assertions describe the temporal behavior of a clock signal."
        out = strip(raw)
        self.assertIn("clock signal", out)
        self.assertGreater(len(out), 30)

    def test_preserves_acronym_expansions(self):
        # We DELIBERATELY do not strip "FOO (Full Expansion)" patterns in
        # v1 because some are real (e.g. on first introduction in a spec).
        raw = """The PTOPO MIB (optional) is connected to the system.
LSAP (Link Service Access Point) is defined in the standard."""
        out = strip(raw)
        self.assertIn("(optional)", out)
        self.assertIn("(Link Service Access Point)", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
