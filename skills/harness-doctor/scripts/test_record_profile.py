#!/usr/bin/env python3
"""Tests for record_profile.py — the Tier-2 skill-side [harness] writer.

This is the ONLY write_text in scaffold-doctor #03 (AC15: the read-only #02
engine never mutates; the write lives here). The load-bearing invariant (AC16)
is a non-destructive, comment-preserving, section-scoped merge — never a lossy
tomllib→writer rewrite. unittest to match the repo's other suites.
"""
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                            # record_profile (this dir)
sys.path.insert(0, str(_HERE.parents[2] / "scripts"))    # harness_doctor (~/.claude/scripts)

import harness_doctor
import record_profile


def _new_repo(toml: str = None) -> Path:
    d = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    if toml is not None:
        (d / "harness.toml").write_text(toml, encoding="utf-8")
    return d


class TestRecordProfile(unittest.TestCase):
    def test_writes_parseable_harness_section_into_fresh_repo(self):
        # AC10: a fresh repo with no harness.toml gets one carrying the recorded
        # intended scaffold + ci judgment, and it round-trips through tomllib.
        repo = _new_repo()
        record_profile.record_profile(
            repo, {"scaffold": ["agents-md", "status-harness", "merge-gate"], "ci": True})
        data = tomllib.loads((repo / "harness.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["harness"]["scaffold"],
                         ["agents-md", "status-harness", "merge-gate"])
        self.assertEqual(data["harness"]["ci"], True)

    def test_merge_preserves_sibling_section_inline_comments(self):
        # AC16 footgun: the write is section-scoped. Sibling gate sections AND
        # their inline comments survive byte-for-byte (the real target repos +
        # ~/.claude/harness.toml are richly annotated); a lossy whole-file rewrite
        # would drop them. Append path: no prior [harness].
        existing = (
            "[merge-gate]\n"
            'profile = "local"   # selected by the installer\n'
            "\n"
            "[merge-gate.local]\n"
            'enforcement_policy = "advisory"   # advisory | client-side-blocking\n'
            'review_globs       = ["**/*"]     # everything in scope\n'
        )
        repo = _new_repo(existing)
        record_profile.record_profile(repo, {"scaffold": ["agents-md", "merge-gate"], "ci": False})
        out = (repo / "harness.toml").read_text(encoding="utf-8")
        for line in existing.splitlines():
            self.assertIn(line, out)        # every original line incl. comments survives
        data = tomllib.loads(out)           # whole file still parses
        self.assertEqual(data["harness"]["scaffold"], ["agents-md", "merge-gate"])
        self.assertEqual(data["merge-gate"]["profile"], "local")          # sibling intact
        self.assertEqual(data["merge-gate"]["local"]["enforcement_policy"], "advisory")

    def test_replace_existing_harness_before_sibling_stays_valid_and_keeps_comments(self):
        # AC16 on the re-record path (panel: the dangerous branch). A STALE
        # [harness] sitting BEFORE a commented sibling must be replaced (not
        # duplicated), the sibling's inline comment must survive byte-for-byte,
        # and the result must remain valid TOML (no section header glued onto a
        # value line — the invalid-TOML-on-replace footgun).
        existing = (
            "[harness]\n"
            'scaffold = ["agents-md"]\n'      # stale — must be replaced
            "ci = false\n"
            "\n"
            "[merge-gate]\n"
            'profile = "local"   # do not clobber this comment\n'
        )
        repo = _new_repo(existing)
        record_profile.record_profile(
            repo, {"scaffold": ["agents-md", "status-harness", "merge-gate"], "ci": True})
        out = (repo / "harness.toml").read_text(encoding="utf-8")
        data = tomllib.loads(out)            # raises on a duplicate table or glued header
        self.assertEqual(data["harness"]["scaffold"],
                         ["agents-md", "status-harness", "merge-gate"])   # replaced
        self.assertEqual(data["harness"]["ci"], True)
        self.assertEqual(out.count("[harness]\n"), 1)                     # not duplicated
        self.assertIn('profile = "local"   # do not clobber this comment', out)
        self.assertEqual(data["merge-gate"]["profile"], "local")

    def test_spaced_harness_header_is_replaced_not_duplicated(self):
        # codex:finding-0 (high): a VALID existing `[ harness ]` (spaces — TOML-
        # equivalent to [harness]) was kept by the exact-string drop, then a second
        # canonical [harness] was appended → TOMLDecodeError "declared twice", and
        # the doctor then reads {} and mis-reports. Re-record must recognise the
        # equivalent header, replace it, and keep the file valid + comments intact.
        existing = (
            "[ harness ]\n"
            'scaffold = ["agents-md"]\n'      # stale, spaced header — must be replaced
            "\n"
            "[merge-gate]\n"
            'profile = "local"   # keep this comment\n'
        )
        repo = _new_repo(existing)
        record_profile.record_profile(
            repo, {"scaffold": ["agents-md", "merge-gate"], "ci": True})
        out = (repo / "harness.toml").read_text(encoding="utf-8")
        data = tomllib.loads(out)            # raises on a duplicate [harness] table
        self.assertEqual(data["harness"]["scaffold"], ["agents-md", "merge-gate"])  # replaced
        self.assertEqual(data["merge-gate"]["profile"], "local")                    # sibling intact
        self.assertIn("# keep this comment", out)

    def test_quoted_harness_header_is_replaced_not_duplicated(self):
        # Same footgun via a quoted-key table header (`["harness"]`, TOML-equivalent
        # to [harness]): it must be the block replaced, never duplicated.
        repo = _new_repo('["harness"]\nscaffold = ["agents-md"]\n')
        record_profile.record_profile(repo, {"scaffold": ["merge-gate"], "ci": False})
        out = (repo / "harness.toml").read_text(encoding="utf-8")
        data = tomllib.loads(out)
        self.assertEqual(data["harness"]["scaffold"], ["merge-gate"])

    def test_array_of_tables_after_harness_not_dropped_on_rerecord(self):
        # Codex toml_sections:24 (high, reproduced): a `[[…]]` array-of-tables
        # FOLLOWING the [harness] block was absorbed into it (header() returned
        # None for `[[…]]`), so dropping the [harness] block on re-record
        # silently deleted the array — data loss. The array must now survive as
        # its own block, and the file must stay valid TOML.
        existing = (
            "[harness]\n"
            'scaffold = ["agents-md"]\n'
            "\n"
            "[[finding]]\n"
            'id = "F1"\n'
            'note = "keep me"\n'
            "\n"
            "[[finding]]\n"
            'id = "F2"\n'
        )
        repo = _new_repo(existing)
        record_profile.record_profile(
            repo, {"scaffold": ["agents-md", "merge-gate"], "ci": True})
        out = (repo / "harness.toml").read_text(encoding="utf-8")
        data = tomllib.loads(out)                      # raises if a block was glued/lost
        self.assertEqual([f["id"] for f in data["finding"]], ["F1", "F2"])  # both survived
        self.assertEqual(data["finding"][0]["note"], "keep me")
        self.assertEqual(data["harness"]["scaffold"], ["agents-md", "merge-gate"])

    def test_round_trip_record_then_engine_reads_and_computes_coverage(self):
        # A10↔A13 integration across the writer→engine boundary: a recorded
        # profile is read back by the read-only engine and drives coverage. Here
        # agents-md is present (AGENTS.md + @AGENTS.md), status-harness and
        # merge-gate are absent → 1/3, ci echoed from the record.
        repo = _new_repo()
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        record_profile.record_profile(
            repo, {"scaffold": ["agents-md", "status-harness", "merge-gate"], "ci": True})
        prof = harness_doctor.read_recorded_profile(repo)
        self.assertEqual(prof["scaffold"], ["agents-md", "status-harness", "merge-gate"])
        cov = harness_doctor.compute_coverage(harness_doctor.diagnose(repo), prof)
        self.assertEqual(cov["applicable"], ["agents-md", "merge-gate", "status-harness"])
        self.assertIn("agents-md", cov["covered"])
        self.assertEqual(cov["fraction"], (1, 3))
        self.assertEqual(cov["judgments"]["ci"], True)


class TestAtomicWrite(unittest.TestCase):
    # claude:finding-0: record_profile()'s in-place path.write_text truncates-then-
    # writes, so a crash/kill/full-disk mid-write corrupts the very richly-annotated
    # sibling gate config (AC16's byte-for-byte invariant). The write must be atomic:
    # temp file in the same dir + os.replace, so a failure leaves the ORIGINAL intact.
    def test_atomic_write_replaces_content_no_tmp_litter(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "harness.toml"
            p.write_text("old\n", encoding="utf-8")
            record_profile._atomic_write_text(p, "new\n")
            self.assertEqual(p.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(list(Path(d).glob(".harness.toml.tmp-*")), [])

    def test_interrupted_write_preserves_original_sibling_config(self):
        # The data-loss regression: a torn write (forced here by a lone surrogate the
        # strict-UTF-8 encode rejects) must leave the annotated sibling gate config
        # byte-for-byte intact — never truncated/empty — and leave no temp litter.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "harness.toml"
            original = '[merge-gate]\nprofile = "local"   # must survive\n'
            p.write_text(original, encoding="utf-8")
            with self.assertRaises(UnicodeEncodeError):
                record_profile._atomic_write_text(p, "x\udce9y")  # lone surrogate
            self.assertEqual(p.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(d).glob(".harness.toml.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
