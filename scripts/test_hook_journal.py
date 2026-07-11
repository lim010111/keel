#!/usr/bin/env python3
"""Unit tests for hook_journal.py (harness-journal#01).

Stdlib `unittest` only — pytest is not installed in this environment.
Run:  python3 scripts/test_hook_journal.py -v

Isolation: every test redirects the journal via HOOK_JOURNAL_PATH to a temp
file, so the real journal at ~/.claude/hooks/.hook-journal.jsonl is never
touched. Other test modules in this repo set HOOK_JOURNAL_DISABLED=1 at
import (so hook subprocesses under test never spam the real journal) and
unittest discover imports every module into ONE process — so setUp here must
scrub and restore the HOOK_JOURNAL_* env itself.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
HOOKS = SCRIPTS.parent / "hooks"
sys.path.insert(0, str(SCRIPTS))
import hook_journal  # noqa: E402

ENV_KEYS = ("HOOK_JOURNAL_PATH", "HOOK_JOURNAL_MAX_BYTES", "HOOK_JOURNAL_DISABLED")


class JournalTest(unittest.TestCase):
    """Base: temp journal path per test + full HOOK_JOURNAL_* env restore."""

    def setUp(self):
        saved = {k: os.environ.get(k) for k in ENV_KEYS}

        def _restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(_restore)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmpdir = Path(td.name)
        self.path = self.tmpdir / "journal.jsonl"
        os.environ["HOOK_JOURNAL_PATH"] = str(self.path)

    def lines(self, path=None):
        path = path or self.path
        if not path.exists():
            return []
        return [json.loads(ln) for ln in
                path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestSchema(JournalTest):
    def test_appends_minimal_schema(self):
        hook_journal.journal("tdd_verify", "block", "suite-red",
                             payload={"session_id": "s1", "cwd": "/tmp/repo"})
        lines = self.lines()
        self.assertEqual(len(lines), 1)
        e = lines[0]
        self.assertEqual(sorted(e), ["event", "hook", "reason", "repo",
                                     "session_id", "ts"])
        self.assertEqual(e["hook"], "tdd_verify")
        self.assertEqual(e["event"], "block")
        self.assertEqual(e["reason"], "suite-red")
        self.assertEqual(e["session_id"], "s1")
        self.assertEqual(e["repo"], "/tmp/repo")
        self.assertRegex(e["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_repo_param_overrides_payload_cwd(self):
        hook_journal.journal("h", "pass", "r",
                             payload={"cwd": "/cwd"}, repo="/actual/root")
        self.assertEqual(self.lines()[0]["repo"], "/actual/root")

    def test_no_payload_yields_empty_fields(self):
        hook_journal.journal("status", "pass", "stop:unchanged")
        e = self.lines()[0]
        self.assertEqual(e["session_id"], "")
        self.assertEqual(e["repo"], "")

    def test_non_dict_payload_is_tolerated(self):
        hook_journal.journal("h", "skip", "r", payload="garbage")
        self.assertEqual(len(self.lines()), 1)


class TestFailOpen(JournalTest):
    def test_unwritable_parent_never_raises(self):
        blocker = self.tmpdir / "file-not-dir"
        blocker.write_text("x")
        os.environ["HOOK_JOURNAL_PATH"] = str(blocker / "sub" / "j.jsonl")
        hook_journal.journal("h", "fire", "r")  # must not raise

    def test_path_is_a_directory_never_raises(self):
        d = self.tmpdir / "adir"
        d.mkdir()
        os.environ["HOOK_JOURNAL_PATH"] = str(d)
        hook_journal.journal("h", "fire", "r")  # must not raise

    def test_disabled_env_writes_nothing(self):
        os.environ["HOOK_JOURNAL_DISABLED"] = "1"
        hook_journal.journal("h", "fire", "r")
        self.assertFalse(self.path.exists())

    def test_for_hook_callables_never_raise_on_bad_input(self):
        j, d = hook_journal.for_hook("x")
        os.environ["HOOK_JOURNAL_PATH"] = str(self.tmpdir / "adir2")
        (self.tmpdir / "adir2").mkdir()
        j("fire", object(), payload=object())      # must not raise
        d(None, ("session_id",))                   # must not raise


class TestRotation(JournalTest):
    def test_rotates_at_cap_and_stays_bounded(self):
        os.environ["HOOK_JOURNAL_MAX_BYTES"] = "400"
        rotated = Path(str(self.path) + ".1")
        for i in range(50):
            hook_journal.journal("h", "fire", f"event-{i:03d}",
                                 payload={"session_id": "s", "cwd": "/r"})
        self.assertTrue(rotated.exists(), "cap reached but no rotation")
        # Bounded: live file < cap + one line; both files together stay small.
        self.assertLess(self.path.stat().st_size, 400 + 300)
        self.assertLess(rotated.stat().st_size, 400 + 300)
        # The newest event is always in the live file (rotation never drops
        # the event that triggered it).
        self.assertEqual(self.lines()[-1]["reason"], "event-049")

    def test_no_event_lost_across_first_rotation(self):
        # Cap sized so the 3rd write triggers rotation: e0+e1 land in .1,
        # e2 starts the fresh file — all three survive.
        probe = json.dumps({
            "ts": "2026-07-11T00:00:00+09:00", "hook": "h", "event": "fire",
            "reason": "e0", "session_id": "s", "repo": "/r"}) + "\n"
        os.environ["HOOK_JOURNAL_MAX_BYTES"] = str(int(len(probe) * 2))
        for r in ("e0", "e1", "e2"):
            hook_journal.journal("h", "fire", r,
                                 payload={"session_id": "s", "cwd": "/r"})
        rotated = Path(str(self.path) + ".1")
        got = [e["reason"] for e in self.lines(rotated)] + \
              [e["reason"] for e in self.lines()]
        self.assertEqual(got, ["e0", "e1", "e2"])


class TestDriftCounter(JournalTest):
    def test_missing_fields_journal_a_drift_event(self):
        missing = hook_journal.check_payload(
            "tdd_verify", {"session_id": "s1"},
            ("session_id", "cwd", "transcript_path", "stop_hook_active"))
        self.assertEqual(missing, ["cwd", "transcript_path", "stop_hook_active"])
        lines = self.lines()
        self.assertEqual(len(lines), 1)
        e = lines[0]
        self.assertEqual(e["event"], "drift")
        self.assertEqual(e["reason"],
                         "missing:cwd,transcript_path,stop_hook_active")
        self.assertEqual(e["session_id"], "s1")

    def test_complete_payload_writes_nothing(self):
        missing = hook_journal.check_payload(
            "h", {"session_id": "s", "cwd": "/r"}, ("session_id", "cwd"))
        self.assertEqual(missing, [])
        self.assertFalse(self.path.exists())

    def test_key_absence_only_empty_value_is_not_drift(self):
        missing = hook_journal.check_payload(
            "h", {"session_id": ""}, ("session_id",))
        self.assertEqual(missing, [])
        self.assertFalse(self.path.exists())

    def test_non_dict_payload_counts_all_fields_missing(self):
        missing = hook_journal.check_payload("h", None, ("session_id", "cwd"))
        self.assertEqual(missing, ["session_id", "cwd"])
        self.assertEqual(self.lines()[0]["reason"], "missing:session_id,cwd")


class TestForHook(JournalTest):
    def test_binds_hook_name(self):
        j, d = hook_journal.for_hook("grill_pause")
        j("fire", "paused:harden-issue", payload={"session_id": "s"})
        d({"session_id": "s"}, ("session_id", "cwd"))
        hooks = [e["hook"] for e in self.lines()]
        self.assertEqual(hooks, ["grill_pause", "grill_pause"])


class TestCli(JournalTest):
    """The __main__ path shell hooks use (scripts/sound_permission.sh)."""

    def _env(self):
        env = {k: v for k, v in os.environ.items()
               if k != "HOOK_JOURNAL_DISABLED"}
        env["HOOK_JOURNAL_PATH"] = str(self.path)
        return env

    def test_cli_journals_event_and_drift(self):
        r = subprocess.run(
            ["python3", str(SCRIPTS / "hook_journal.py"),
             "sound_permission", "fire", "played", "session_id,cwd,tool_name"],
            input=json.dumps({"session_id": "s9", "tool_name": "Bash"}),
            capture_output=True, text=True, env=self._env(), timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        events = [(e["event"], e["reason"]) for e in self.lines()]
        self.assertEqual(events, [("drift", "missing:cwd"),
                                  ("fire", "played")])

    def test_cli_garbage_stdin_still_exits_zero(self):
        r = subprocess.run(
            ["python3", str(SCRIPTS / "hook_journal.py"), "h", "fire"],
            input="not json at all", capture_output=True, text=True,
            env=self._env(), timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.lines()[0]["session_id"], "")


class TestHookIntegrationFailOpen(JournalTest):
    """AC: a payload missing expected fields → drift event journaled AND the
    hook's own behaviour stays fail-open (exit 0, baseline stdout)."""

    def test_tdd_guard_missing_fields_drift_plus_fail_open(self):
        payload = {"session_id": "JOURNALTEST-nosuch", "tool_name": "Write"}
        r = subprocess.run(
            ["python3", str(HOOKS / "tdd_guard.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env=self._env(), timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)     # fail-open preserved
        self.assertEqual(r.stdout, "")                  # baseline stdout
        events = {e["event"]: e for e in self.lines()}
        self.assertIn("drift", events)
        self.assertIn("cwd", events["drift"]["reason"])
        self.assertIn("tool_input", events["drift"]["reason"])
        self.assertIn("skip", events)                   # mode-off no-op

    def _env(self):
        env = {k: v for k, v in os.environ.items()
               if k != "HOOK_JOURNAL_DISABLED"}
        env["HOOK_JOURNAL_PATH"] = str(self.path)
        return env


if __name__ == "__main__":
    unittest.main(verbosity=2)
