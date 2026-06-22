#!/usr/bin/env python3
"""Tests for check_alignment_skill_drift.py (self-containment#04).

Stdlib unittest only — pytest is not installed in this environment.
Run:  python3 scripts/test_check_alignment_skill_drift.py -v

The script recomputes each watched alignment skill's git-tree-SHA (the same
object hash the `skills` CLI records as `skillFolderHash` — it is the upstream
git tree SHA, not the sha256 local hash) and compares it to the recorded value
in ~/.agents/.skill-lock.json. Advisory only: it never exits non-zero in a way
that gates a session and surfaces a line only on drift (ADR-0031 §B step 4).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import check_alignment_skill_drift as drift  # noqa: E402

SCRIPT = SCRIPTS / "check_alignment_skill_drift.py"
AGENTS = Path.home() / ".agents"


def write_lock(agents_dir: Path, entries: dict) -> None:
    """entries: {skill_name: skillFolderHash}."""
    skills = {name: {"source": "mattpocock/skills", "skillFolderHash": h}
              for name, h in entries.items()}
    (agents_dir / ".skill-lock.json").write_text(
        json.dumps({"version": 3, "skills": skills}), encoding="utf-8")


def make_skill(agents_dir: Path, name: str, body: str = "# skill\n") -> Path:
    d = agents_dir / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def run_script(*, agents_dir):
    env = {**os.environ, "ALIGNMENT_DRIFT_AGENTS_DIR": str(agents_dir)}
    return subprocess.run(["python3", str(SCRIPT)],
                          capture_output=True, text=True, env=env, timeout=60)


# --------------------------------------------------------------------------
# correctness anchor — the pure-Python git-tree-SHA matches git's own object
# hash, proven against grill-me's externally-known recorded value.
# --------------------------------------------------------------------------
class TestHashMatchesGit(unittest.TestCase):
    @unittest.skipUnless((AGENTS / "skills" / "grill-me").is_dir(),
                         "live ~/.agents/skills/grill-me not present")
    def test_grill_me_hash_equals_recorded_git_tree_sha(self):
        # 2a1ad170… is the git tree SHA the skills CLI recorded for the pristine
        # grill-me folder (verified equal to `git write-tree`). If our pure-Python
        # implementation matches it, it matches git.
        got = drift.folder_hash(AGENTS / "skills" / "grill-me")
        self.assertEqual(got, "2a1ad17028306ebe45f0e49703fa28b9b2e7f499",
                         "folder_hash must reproduce git's tree object SHA")


# --------------------------------------------------------------------------
# AC1 — drift when live != recorded; clean otherwise (throwaway copies)
# --------------------------------------------------------------------------
class TestDriftDetection(unittest.TestCase):
    def test_clean_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            make_skill(ad, "grill-with-docs", "# grill-with-docs\n\nbody\n")
            recorded = drift.folder_hash(ad / "skills" / "grill-with-docs")
            write_lock(ad, {"grill-with-docs": recorded})
            self.assertEqual(drift.find_drift(ad), [],
                             "matching hash must report no drift")

    def test_drift_when_folder_changes(self):
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            folder = make_skill(ad, "grill-with-docs", "# grill-with-docs\n\nbody\n")
            recorded = drift.folder_hash(folder)
            write_lock(ad, {"grill-with-docs": recorded})
            # An upstream/local edit changes the folder content -> hash moves.
            (folder / "SKILL.md").write_text("# grill-with-docs\n\nTAMPERED\n",
                                             encoding="utf-8")
            d = drift.find_drift(ad)
            self.assertEqual(len(d), 1, f"a changed folder must report drift: {d}")
            self.assertEqual(d[0]["skill"], "grill-with-docs")


# --------------------------------------------------------------------------
# AC3 — explicit watched set: only the alignment-gate skills, not "all skills"
# --------------------------------------------------------------------------
class TestExplicitWatchedSet(unittest.TestCase):
    def test_watched_set_is_the_alignment_skills(self):
        self.assertIn("grill-me", drift.WATCHED)
        self.assertIn("grill-with-docs", drift.WATCHED)

    def test_unwatched_skill_drift_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            # A non-alignment skill with a deliberately wrong recorded hash.
            make_skill(ad, "some-other-skill", "# other\n")
            write_lock(ad, {"some-other-skill": "0" * 40})
            self.assertEqual(drift.find_drift(ad), [],
                             "drift on a non-watched skill must be ignored")


# --------------------------------------------------------------------------
# AC2 — advisory only: always exit 0; a line on stdout ONLY when there is drift
# --------------------------------------------------------------------------
class TestAdvisoryContract(unittest.TestCase):
    def test_clean_exits_zero_and_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            make_skill(ad, "grill-with-docs")
            make_skill(ad, "grill-me")
            write_lock(ad, {
                "grill-with-docs": drift.folder_hash(ad / "skills" / "grill-with-docs"),
                "grill-me": drift.folder_hash(ad / "skills" / "grill-me"),
            })
            r = run_script(agents_dir=ad)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "", "clean state must be silent")

    def test_drift_exits_zero_and_surfaces_a_line(self):
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            folder = make_skill(ad, "grill-with-docs")
            write_lock(ad, {"grill-with-docs": drift.folder_hash(folder)})
            (folder / "SKILL.md").write_text("# tampered\n", encoding="utf-8")
            r = run_script(agents_dir=ad)
            self.assertEqual(r.returncode, 0,
                             f"advisory must never gate a session; stderr={r.stderr}")
            self.assertIn("grill-with-docs", r.stdout,
                          "drift must surface a line naming the skill")

    def test_missing_lock_is_silent_failsoft(self):
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            make_skill(ad, "grill-with-docs")  # no .skill-lock.json at all
            r = run_script(agents_dir=ad)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "",
                             "a missing lock must fail soft and silent")


# --------------------------------------------------------------------------
# AC4 — against the LIVE ~/.agents, grill-with-docs reports no drift (confirms
# the #02 pristine re-baseline).
# --------------------------------------------------------------------------
class TestLiveReBaseline(unittest.TestCase):
    @unittest.skipUnless((AGENTS / ".skill-lock.json").exists()
                         and (AGENTS / "skills" / "grill-with-docs").is_dir(),
                         "live ~/.agents not present")
    def test_live_grill_with_docs_no_drift(self):
        d = drift.find_drift(AGENTS)
        names = [e["skill"] for e in d]
        self.assertNotIn("grill-with-docs", names,
                         f"#02 restored grill-with-docs pristine — must be clean: {d}")


# --------------------------------------------------------------------------
# wiring — settings.json runs the check on SessionStart (advisory surfacing)
# --------------------------------------------------------------------------
class TestSessionStartWiring(unittest.TestCase):
    def test_sessionstart_runs_the_drift_check(self):
        cfg = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        cmds = [h.get("command", "")
                for e in cfg["hooks"].get("SessionStart", [])
                for h in e.get("hooks", [])]
        self.assertTrue(any("check_alignment_skill_drift.py" in c for c in cmds),
                        f"SessionStart must run the drift check: {cmds}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
