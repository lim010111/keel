#!/usr/bin/env python3
"""Tests for tdd_paths — the single owner of the TDD path classifier.

tdd_guard (blocks) and tdd_mark (records) must agree on what counts as a
test vs an implementation file; before tdd_paths each carried its own
verbatim copy of this policy.
"""
import unittest

import tdd_paths


class TestIsTestFile(unittest.TestCase):
    def test_dot_test_and_spec_infixes(self):
        self.assertTrue(tdd_paths.is_test_file("src/foo.test.ts"))
        self.assertTrue(tdd_paths.is_test_file("src/foo.spec.js"))

    def test_prefix_and_suffix_conventions(self):
        self.assertTrue(tdd_paths.is_test_file("scripts/test_foo.py"))
        self.assertTrue(tdd_paths.is_test_file("pkg/foo_test.go"))

    def test_test_directories(self):
        self.assertTrue(tdd_paths.is_test_file("proj/tests/helper.py"))
        self.assertTrue(tdd_paths.is_test_file("src/__tests__/x.js"))

    def test_implementation_files_are_not_tests(self):
        self.assertFalse(tdd_paths.is_test_file("src/contest.py"))
        self.assertFalse(tdd_paths.is_test_file("scripts/status.py"))

    def test_windows_separators_normalized(self):
        self.assertTrue(tdd_paths.is_test_file("src\\tests\\x.py"))


class TestIsCodeFile(unittest.TestCase):
    def test_code_extensions(self):
        self.assertTrue(tdd_paths.is_code_file("a/b.py"))
        self.assertTrue(tdd_paths.is_code_file("a/b.RS"))

    def test_docs_and_config_are_not_code(self):
        self.assertFalse(tdd_paths.is_code_file("README.md"))
        self.assertFalse(tdd_paths.is_code_file("settings.json"))
        self.assertFalse(tdd_paths.is_code_file("noext"))


if __name__ == "__main__":
    unittest.main()
