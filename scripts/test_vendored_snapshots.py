#!/usr/bin/env python3
"""Drift guard for cross-tree vendored snapshots.

setup-status-harness ships a byte-copy of the canonical STATUS generator
(`scripts/status.py`) as its bundled snapshot
(`skills/setup-status-harness/scripts/status.py`, the installer's BUNDLED
source). Nothing else re-syncs the pair, so an edit to the canonical file
would silently ship a stale generator to every newly scaffolded target repo.
This test pins the pair byte-equal inside the declared oracle.

Scope note: ADR-0030/0036 govern *target-repo* copies (drift there is
report-only / "differs means customization"). This guard is about the source
repo's own skill snapshot, which has no legitimate reason to differ.
"""
import unittest
from pathlib import Path

CLAUDE_DIR = Path(__file__).resolve().parent.parent


class TestStatusSnapshot(unittest.TestCase):
    def test_bundled_status_snapshot_matches_canonical(self):
        canonical = CLAUDE_DIR / "scripts" / "status.py"
        bundled = (CLAUDE_DIR / "skills" / "setup-status-harness"
                   / "scripts" / "status.py")
        self.assertTrue(bundled.is_file(),
                        "bundled snapshot missing — setup-status-harness "
                        "cannot seed target repos")
        self.assertEqual(
            canonical.read_bytes(), bundled.read_bytes(),
            "skills/setup-status-harness/scripts/status.py has drifted from "
            "scripts/status.py — re-sync the snapshot (cp scripts/status.py "
            "skills/setup-status-harness/scripts/status.py) so the installer "
            "does not ship a stale generator",
        )


if __name__ == "__main__":
    unittest.main()
