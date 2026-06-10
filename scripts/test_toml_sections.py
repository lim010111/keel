#!/usr/bin/env python3
"""Tests for toml_sections — the shared section-scoped TOML text utilities.

The byte-fidelity and header-equivalence behaviour is ALSO covered end-to-end
through its two consumers (test_record_profile, test_install_local); these
tests pin the module's own contract directly.
"""
import unittest

import toml_sections as ts


class TestHeader(unittest.TestCase):
    def test_plain_header(self):
        self.assertEqual(ts.header("[merge-gate]\n"), "[merge-gate]")

    def test_indented_header(self):
        self.assertEqual(ts.header("  [harness]  # c\n"), "[harness]")

    def test_value_line_is_not_a_header(self):
        self.assertIsNone(ts.header('scaffold = ["agents-md"]\n'))

    def test_array_of_tables_starts_its_own_block(self):
        # `[[x]]` IS a block boundary (so it is never absorbed into — and
        # dropped with — a preceding deletable block; Codex toml_sections:24).
        # It is NOT, however, a single-bracket table for section_is/name.
        self.assertEqual(ts.header("[[finding]]\n"), "[[finding]]")


class TestSplitSections(unittest.TestCase):
    def test_rejoins_byte_for_byte(self):
        text = "# preamble\n\n[a]\nx = 1  # keep\n\n[ b ]\ny = 2\n"
        blocks = ts.split_sections(text)
        self.assertEqual("".join("".join(lines) for _, lines in blocks), text)

    def test_preamble_block_has_none_header(self):
        blocks = ts.split_sections("# top\n[a]\n")
        self.assertIsNone(blocks[0][0])
        self.assertEqual(blocks[1][0], "[a]")


class TestSectionMatching(unittest.TestCase):
    def test_equivalent_spellings_match(self):
        for hdr in ("[harness]", "[ harness ]", '["harness"]', "['harness']"):
            self.assertTrue(ts.section_is(hdr, "harness"), hdr)

    def test_dotted_names_normalize_per_segment(self):
        self.assertTrue(ts.section_is('[ merge-gate . "local" ]', "merge-gate.local"))
        self.assertEqual(ts.section_name("[merge-gate.local]"), "merge-gate.local")

    def test_different_table_does_not_match(self):
        self.assertFalse(ts.section_is("[merge-gate]", "harness"))
        self.assertFalse(ts.section_is("[merge-gate.local]", "merge-gate"))

    def test_none_header_matches_nothing(self):
        self.assertFalse(ts.section_is(None, "harness"))
        self.assertIsNone(ts.section_name(None))

    def test_array_of_tables_never_matches_a_table_query(self):
        # `[[merge-gate]]` (array-of-tables) is NOT the `[merge-gate]` table —
        # edit/delete-by-name callers must never touch one.
        self.assertFalse(ts.section_is("[[merge-gate]]", "merge-gate"))
        self.assertFalse(ts.section_is("[[harness]]", "harness"))
        self.assertIsNone(ts.section_name("[[finding]]"))


class TestArrayOfTablesPreserved(unittest.TestCase):
    def test_aot_after_deleted_block_is_not_dropped(self):
        # The split-then-drop pattern record_profile uses: a `[[…]]` block that
        # follows a to-be-dropped `[harness]` must survive, because it is its
        # OWN block now (not absorbed into [harness]).
        text = ('[harness]\nscaffold = ["a"]\n\n'
                '[[finding]]\nid = "F1"\n')
        kept = "".join("".join(lines) for hdr, lines in ts.split_sections(text)
                       if not ts.section_is(hdr, "harness"))
        self.assertIn("[[finding]]", kept)
        self.assertIn('id = "F1"', kept)
        self.assertNotIn("scaffold", kept)   # the [harness] block WAS dropped


if __name__ == "__main__":
    unittest.main()
