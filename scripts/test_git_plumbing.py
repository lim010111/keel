#!/usr/bin/env python3
"""Tests for git_plumbing — the shared never-raise git wrappers.

These five wrappers used to exist as verbatim twins in merge_gate_local.py
and leak_scan.py; behaviour is pinned here once. The consumers' own suites
keep exercising them end-to-end through produce/scan flows.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

import git_plumbing as gp


def _run(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True)


class GitRepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _run(self.root, "init", "-q")
        _run(self.root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "--allow-empty", "-q", "-m", "init")


class TestGit(GitRepoCase):
    def test_success_returns_zero_and_text(self):
        rc, out = gp.git(self.root, ["rev-parse", "--show-toplevel"])
        self.assertEqual(rc, 0)
        self.assertEqual(Path(out.strip()), self.root.resolve())

    def test_failure_returns_nonzero_never_raises(self):
        rc, _ = gp.git(self.root, ["rev-parse", "--verify", "no-such-ref"])
        self.assertNotEqual(rc, 0)

    def test_unusable_cwd_never_raises(self):
        rc, _ = gp.git(self.root / "missing-subdir", ["status"])
        self.assertNotEqual(rc, 0)

    def test_git_bytes_returns_raw_bytes(self):
        rc, out = gp.git_bytes(self.root, ["log", "-1", "--format=%H"])
        self.assertEqual(rc, 0)
        self.assertIsInstance(out, bytes)


class TestRepoRootAndRevParse(GitRepoCase):
    def test_repo_root_resolves_toplevel(self):
        self.assertEqual(gp.repo_root(self.root), self.root.resolve())

    def test_repo_root_none_outside_a_repo(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertIsNone(gp.repo_root(Path(t)))

    def test_rev_parse_head_and_missing_ref(self):
        sha = gp.rev_parse(self.root, "HEAD")
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertIsNone(gp.rev_parse(self.root, "no-such-ref"))


class TestTipBypassReason(GitRepoCase):
    TRAILER = "Merge-Gate-Bypass"

    def _commit(self, msg):
        _run(self.root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "--allow-empty", "-q", "-m", msg)

    def test_no_trailer_is_none(self):
        self._commit("plain commit")
        self.assertIsNone(
            gp.tip_bypass_reason(self.root, "HEAD", self.TRAILER))

    def test_trailer_reason_is_returned(self):
        self._commit(f"msg\n\n{self.TRAILER}: hotfix, reviewed by hand")
        self.assertEqual(
            gp.tip_bypass_reason(self.root, "HEAD", self.TRAILER),
            "hotfix, reviewed by hand")

    def test_empty_trailer_value_is_none(self):
        self._commit(f"msg\n\n{self.TRAILER}:")
        self.assertIsNone(
            gp.tip_bypass_reason(self.root, "HEAD", self.TRAILER))


if __name__ == "__main__":
    unittest.main()
