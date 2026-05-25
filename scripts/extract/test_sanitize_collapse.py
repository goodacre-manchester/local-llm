#!/usr/bin/env python3
"""
Tests for sanitize_collapse.py using real fixtures excerpted from the
2026-05-25 IEEE Parse sidecar scan.

Run with: python test_sanitize_collapse.py
(no pytest dep — pure stdlib unittest.)
"""

from __future__ import annotations

import unittest

from sanitize_collapse import (
    detect_collapse,
    sanitize_block,
    sanitize_sidecar,
)


class TestDetectCollapse(unittest.TestCase):
    """Each test uses a real-world cascade shape observed in the scan."""

    def test_s_token_cascade(self):
        # From 8021AB-2016 block #5 tail: hundreds of (S) tokens
        text = "Some legitimate intro text. " + "(S) " * 100
        pos = detect_collapse(text)
        self.assertIsNotNone(pos)
        # Cascade starts roughly where the (S) repetition begins
        self.assertLess(pos, 100)

    def test_dashed_line_cascade(self):
        # From 8021AS-2025 block #5215: \------ repeating
        text = "Real content about PTP. " + "\\------ " * 60
        pos = detect_collapse(text)
        self.assertIsNotNone(pos)

    def test_dot_leader_cascade(self):
        # From 8021CBcv-2021 + 8021Qat-2010: ... ... ... ...
        # Real cascades come embedded in much longer blocks (17-23 KB);
        # use a realistic-length prefix so the min-length guard passes.
        text = "Real content from a Parse-extracted block. " * 10 + ". " * 80 + "tail"
        pos = detect_collapse(text)
        self.assertIsNotNone(pos)

    def test_triple_backtick_cascade(self):
        # From 8021CBdb-2021 blocks: ``` ``` ``` ``` cascade
        text = "Real prose content here for testing purposes. " + "``` " * 50
        pos = detect_collapse(text)
        self.assertIsNotNone(pos)

    def test_clean_text_returns_none(self):
        # Real prose, no cascade — must not flag
        text = (
            "The diagram shows the LLDP architecture. Two components are connected: "
            "the LLDP agent and the LLC entity. Each LLDP agent contains an LSAP "
            "and an MSAP. The LLDP management entity supervises multiple LLDP "
            "agents. Optional MIB extensions can be defined per device."
        ) * 5  # pad to >500 chars so the min-length check passes
        self.assertIsNone(detect_collapse(text))

    def test_short_text_skipped(self):
        # < 500 chars: do not even scan — too risky to false-positive
        text = "(S) (S) (S) (S) (S)"  # legitimate-looking short snippet
        self.assertIsNone(detect_collapse(text))

    def test_clean_table_not_flagged(self):
        # GFM-style table — has repeated `|` chars but NOT cascade-shape
        text = """
| Header A | Header B | Header C |
| -------- | -------- | -------- |
| value 1  | value 2  | value 3  |
| value 4  | value 5  | value 6  |
| value 7  | value 8  | value 9  |
""" * 20  # pad
        self.assertIsNone(detect_collapse(text))


class TestSanitizeBlock(unittest.TestCase):
    def test_preserves_legitimate_prefix(self):
        prefix = "This is real legitimate text about IEEE standards. " * 20
        cascade = "(S) " * 100
        cleaned, truncated = sanitize_block(prefix + cascade)
        self.assertTrue(truncated)
        self.assertIn("IEEE standards", cleaned)
        self.assertIn("collapse-truncated", cleaned)

    def test_clean_block_unchanged(self):
        clean = "Real prose paragraph about something. " * 30
        cleaned, truncated = sanitize_block(clean)
        self.assertFalse(truncated)
        self.assertEqual(cleaned, clean)

    def test_empty_input(self):
        cleaned, truncated = sanitize_block("")
        self.assertFalse(truncated)
        self.assertEqual(cleaned, "")

    def test_idempotent(self):
        text = "Real text. " * 30 + "(S) " * 100
        once, _ = sanitize_block(text)
        twice, was_truncated = sanitize_block(once)
        self.assertFalse(was_truncated, "re-sanitizing should be a no-op")
        self.assertEqual(once, twice)


class TestRealFixture(unittest.TestCase):
    """Reduced fixture from the actual 8021AB-2016 block #5 corruption."""

    def test_gmail_cascade_fixture(self):
        # Reduced version: the real fixture had 99 @gmail.com hallucinations
        # + 790 (S) tokens; this is enough to trigger detection.
        text = (
            "IEEE <sup>∗</sup>Electronic address: `johnson@gmail.com` "
            "(Daniel P. R. Smith) "
            "<sup>†</sup>Electronic address: `david@gmail.com` "
            "(Sarah M. K. Davis) "
            + "\\(^\\unknown\\)Electronic address: `david@gmail.com` "
              "(Sarah M. K. Davis) " * 30
            + "(S)(S)(S) " * 100
        )
        cleaned, truncated = sanitize_block(text)
        self.assertTrue(truncated)
        # The hallucinated cascade must be gone
        self.assertNotIn("(S)(S)(S)", cleaned)
        # The fake author entries should also be gone — the
        # "Electronic address" pattern is a hallucination signal even
        # before the (S) cascade kicks in.
        self.assertNotIn("@gmail.com", cleaned)
        # Length should drop dramatically
        self.assertLess(len(cleaned), len(text) * 0.5)


class TestSanitizeSidecar(unittest.TestCase):
    def test_modifies_dirty_blocks_only(self):
        sidecar = {
            "doc": "test.pdf",
            "backend": "nemotron-parse-v1.2",
            "blocks": [
                {"id": "p1-b0", "page": 1, "type": "text",
                 "text": "Clean prose paragraph. " * 30},
                {"id": "p1-b1", "page": 1, "type": "text",
                 "text": "Some intro. " * 20 + "(S) " * 100},
                {"id": "p2-b0", "page": 2, "type": "heading",
                 "text": "Section 1.2 Overview"},
            ],
        }
        _, changes = sanitize_sidecar(sidecar)
        # Only the dirty block was changed
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["block_id"], "p1-b1")
        # The clean block + heading are untouched
        self.assertIn("Clean prose paragraph", sidecar["blocks"][0]["text"])
        self.assertEqual(sidecar["blocks"][2]["text"], "Section 1.2 Overview")
        # The dirty block has its cascade removed
        self.assertNotIn("(S) (S) (S)", sidecar["blocks"][1]["text"])
        self.assertIn("collapse-truncated", sidecar["blocks"][1]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
