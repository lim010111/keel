#!/usr/bin/env python3
"""Tests for run_skill_tests — the vendored-skill suite runner.

The declared oracle (.claude/tdd-test-cmd) discovers scripts/ and hooks/
only; authored skill suites under skills/*/scripts/ are collected by this
runner. Rented skills (symlinked into ~/.agents) are never ours to run.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "run_skill_tests.py"

PASSING_TEST = """\
import unittest

class T(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)

if __name__ == "__main__":
    unittest.main()
"""

FAILING_TEST = """\
import unittest

class T(unittest.TestCase):
    def test_no(self):
        self.fail("deliberate red")

if __name__ == "__main__":
    unittest.main()
"""


def make_tree(root: Path) -> Path:
    """A fake ~/.claude with one vendored suite, one nested-layout suite,
    one rented (symlinked) skill carrying a would-fail test, and one
    scriptless skill."""
    skills = root / "skills"
    (skills / "vendored-a" / "scripts").mkdir(parents=True)
    (skills / "vendored-a" / "scripts" / "test_a.py").write_text(PASSING_TEST)
    (skills / "vendored-b" / "scripts" / "tests").mkdir(parents=True)
    (skills / "vendored-b" / "scripts" / "tests" / "test_b.py").write_text(
        PASSING_TEST)
    (skills / "no-tests" / "scripts").mkdir(parents=True)
    (skills / "no-tests" / "scripts" / "helper.py").write_text("x = 1\n")
    rented_src = root / ".agents-skill"
    (rented_src / "scripts").mkdir(parents=True)
    (rented_src / "scripts" / "test_rented.py").write_text(FAILING_TEST)
    os.symlink(rented_src, skills / "rented")
    return root


class TestSuiteDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_tree(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def test_collects_vendored_layouts_and_skips_symlinks(self):
        sys.path.insert(0, str(HERE))
        self.addCleanup(sys.path.remove, str(HERE))
        import run_skill_tests as rst
        got = rst.suite_dirs(self.root)
        rel = sorted(str(p.relative_to(self.root)) for p in got)
        self.assertEqual(rel, [
            "skills/vendored-a/scripts",
            "skills/vendored-b/scripts/tests",
        ])


class TestRunnerProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_tree(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def run_runner(self):
        return subprocess.run(
            [sys.executable, str(RUNNER), "--claude-dir", str(self.root)],
            capture_output=True, text=True, timeout=120,
        )

    def test_all_green_exits_zero_and_names_each_suite(self):
        r = self.run_runner()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertIn("vendored-a", out)
        self.assertIn("vendored-b", out)
        self.assertNotIn("rented", out)

    def test_one_red_suite_exits_nonzero(self):
        (self.root / "skills" / "vendored-a" / "scripts"
         / "test_a.py").write_text(FAILING_TEST)
        r = self.run_runner()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("vendored-a", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
