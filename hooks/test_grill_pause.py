#!/usr/bin/env python3
"""Tests for grill_pause.py — the owned PreToolUse hook that pauses the
narrative guard on a grill-family Skill launch (self-containment#02).

Stdlib unittest only — pytest is not installed in this environment.
Run:  python3 hooks/test_grill_pause.py -v

ADR-0031 §B step 2 ("own the delta, rent the base"): the narrative_guard pause
that brackets a grilling session used to be written by the RENTED grilling
skills' prose. This hook moves that WRITER to the owned side — it fires on a
grill-family Skill launch and writes the SAME mutable pause marker that
`narrative_guard.py pause` writes, keyed the same (session, repo) way `check`
reads. So `check` (untouched) honours it and `resume` re-arms it.

Isolation mirrors test_narrative_guard.py: every subprocess points the hook's
state dir at a fresh temp via NARRATIVE_GUARD_STATE_ROOT, so the real
~/.claude/hooks/.narrative-guard-state/ is never touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
SCRIPTS = HOOKS.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HOOKS))

# Reuse the narrative_guard test harness (fixtures + check/snapshot/resume
# drivers) so the AC2 integration test exercises the real guard, not a mock.
from test_narrative_guard import (  # noqa: E402
    basic_repo, seed_issue, vid, _snapshot, _check, run_pause, pause_files,
    REAL_STATE, _names,
)

PAUSE_HOOK = HOOKS / "grill_pause.py"


def run_grill_pause(payload: dict, *, state_root):
    env = {**os.environ, "NARRATIVE_GUARD_STATE_ROOT": str(state_root)}
    return subprocess.run(
        ["python3", str(PAUSE_HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=60,
    )


def pre_payload(root, sid, skill, *, tool="Skill"):
    return {"session_id": sid, "cwd": str(root),
            "tool_name": tool, "tool_input": {"skill": skill}}


# --------------------------------------------------------------------------
# isolation guard (defence-in-depth; tests use NARRATIVE_GUARD_STATE_ROOT)
# --------------------------------------------------------------------------
_SNAPSHOT: dict = {}


def setUpModule():
    _SNAPSHOT["real"] = _names(REAL_STATE)


def tearDownModule():
    after = _names(REAL_STATE)
    assert after == _SNAPSHOT["real"], (
        "real .narrative-guard-state changed — a test leaked out of its "
        "NARRATIVE_GUARD_STATE_ROOT sandbox!", _SNAPSHOT["real"], after)


# --------------------------------------------------------------------------
# AC1 — a grill-family Skill launch writes the (session, repo) pause marker
# --------------------------------------------------------------------------
class TestGrillLaunchWritesMarker(unittest.TestCase):
    def test_grill_with_docs_launch_creates_marker(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            r = run_grill_pause(pre_payload(root, sid, "grill-with-docs"), state_root=sr)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(len(pause_files(sr)), 1,
                             f"a grill launch must write one (session, repo) marker; "
                             f"dir={_names(Path(sr))} stderr={r.stderr}")

    def test_both_grill_skills_write_marker(self):
        for skill in ("grill-with-docs", "harden-issue"):
            with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
                root = basic_repo(td, total=2, done=0)
                r = run_grill_pause(pre_payload(root, vid(), skill), state_root=sr)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(len(pause_files(sr)), 1,
                                 f"{skill} launch must write the marker; stderr={r.stderr}")


# --------------------------------------------------------------------------
# AC3 — a non-grill Skill launch (and a non-Skill tool) writes NO marker
# --------------------------------------------------------------------------
class TestNonGrillNoMarker(unittest.TestCase):
    def test_status_skill_does_not_write_marker(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            r = run_grill_pause(pre_payload(root, vid(), "status"), state_root=sr)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(pause_files(sr), [],
                             "a non-grill Skill must not pause the guard")

    def test_grill_me_does_not_pause(self):
        # grill-me is a pure interview skill (no inline ADR/CONTEXT/issue edits) and
        # is deliberately NOT in GRILL_SKILLS: there's nothing for `check` to block
        # during it, and pausing only widened the guard-off window with no re-arm
        # path (it's rented — no /status closing step). d8662367 advisory review.
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            r = run_grill_pause(pre_payload(root, vid(), "grill-me"), state_root=sr)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(pause_files(sr), [],
                             "grill-me must NOT pause the guard (excluded from GRILL_SKILLS)")

    def test_non_skill_tool_does_not_write_marker(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            payload = {"session_id": vid(), "cwd": str(root), "tool_name": "Bash",
                       "tool_input": {"command": "echo grill-with-docs"}}
            r = run_grill_pause(payload, state_root=sr)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(pause_files(sr), [],
                             "a Bash tool that merely mentions a grill skill must not pause")

    def test_never_blocks_the_tool(self):
        # PreToolUse exit 2 would DENY the Skill; this hook must always allow.
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            for skill in ("grill-with-docs", "status"):
                r = run_grill_pause(pre_payload(root, vid(), skill), state_root=sr)
                self.assertEqual(r.returncode, 0,
                                 f"PreToolUse must never deny the tool ({skill}); stderr={r.stderr}")


# --------------------------------------------------------------------------
# AC2 — writer-moved design preserves re-arm: the marker the PreToolUse hook
# wrote suppresses an otherwise-blocking Stop; `resume` re-arms it (the real
# guard, end to end — not a mock).
# --------------------------------------------------------------------------
class TestPauseSuppressesThenResumeReArms(unittest.TestCase):
    def test_hook_marker_suppresses_block_and_resume_rearms(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            # The PreToolUse hook fires as /grill-with-docs launches.
            self.assertEqual(
                run_grill_pause(pre_payload(root, sid, "grill-with-docs"),
                                state_root=sr).returncode, 0)
            self.assertEqual(len(pause_files(sr)), 1, "hook must have written the marker")
            # Mid-grilling posture change: an ADR appears, narrative untouched.
            adr = root / "docs" / "adr"
            adr.mkdir(parents=True)
            adr_file = adr / "0001-x.md"
            adr_file.write_text("# adr one\n", encoding="utf-8")
            r_paused = _check(root, sid, sr, writes=[adr_file])
            self.assertEqual(r_paused.returncode, 0,
                             f"the hook-written marker must suppress the mid-grilling "
                             f"block; stderr={r_paused.stderr}")
            # End of grilling: resume (folded into /status) re-arms the guard;
            # the deferred posture change now legitimately blocks.
            self.assertEqual(run_pause("resume", root, sid, sr).returncode, 0)
            self.assertEqual(pause_files(sr), [], "resume must remove the hook's marker")
            r_armed = _check(root, sid, sr, writes=[adr_file])
            self.assertEqual(r_armed.returncode, 2,
                             f"after resume the real posture change must block; "
                             f"stderr={r_armed.stderr}")


# --------------------------------------------------------------------------
# wiring — settings.json registers the hook on PreToolUse(Skill)
# --------------------------------------------------------------------------
class TestSettingsWiring(unittest.TestCase):
    def test_pretooluse_wires_grill_pause_on_skill(self):
        cfg = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        entries = cfg["hooks"].get("PreToolUse", [])
        wired = [e for e in entries
                 for h in e.get("hooks", [])
                 if "grill_pause.py" in h.get("command", "")]
        self.assertTrue(wired, "PreToolUse must wire grill_pause.py")
        self.assertTrue(any("Skill" in e.get("matcher", "") for e in wired),
                        "grill_pause must be matched on the Skill tool")


# --------------------------------------------------------------------------
# AC4 — /status's closing step re-arms the guard with `resume`, and `resume`
# is a safe no-op when no marker exists.
# --------------------------------------------------------------------------
class TestStatusResumeWiring(unittest.TestCase):
    def test_status_skill_closing_step_calls_resume(self):
        skill = (Path.home() / ".claude" / "skills" / "status" / "SKILL.md").read_text()
        self.assertIn("narrative_guard.py resume", skill,
                      "/status must re-arm the guard in its closing step")

    def test_resume_with_no_marker_is_safe_noop(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            r = run_pause("resume", root, vid(), sr)
            self.assertEqual(r.returncode, 0, f"resume must no-op cleanly; stderr={r.stderr}")
            self.assertEqual(pause_files(sr), [])


# --------------------------------------------------------------------------
# AC6 — back-compat: the explicit `narrative_guard.py pause`/`resume` CLI path
# (the rented skills' old mechanism) still works alongside the owned hook.
# --------------------------------------------------------------------------
class TestManualPauseResumeBackCompat(unittest.TestCase):
    def test_manual_pause_suppresses_and_resume_rearms(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            self.assertEqual(run_pause("pause", root, sid, sr).returncode, 0)
            issue = seed_issue(root, "feat", "01", "alpha", total=2, done=2)
            self.assertEqual(_check(root, sid, sr, writes=[issue]).returncode, 0,
                             "manual pause must still suppress")
            self.assertEqual(run_pause("resume", root, sid, sr).returncode, 0)
            self.assertEqual(_check(root, sid, sr, writes=[issue]).returncode, 2,
                             "manual resume must still re-arm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
