#!/usr/bin/env python3
"""Tests for hook_io — the shared hook-protocol I/O module.

These pin the protocol contract ONCE (stdin read, torn-JSON fallback, session
and cwd resolution, Stop loop-guard, guarded journal handles); the per-hook
subprocess suites in test_tdd_hooks.py / test_narrative_guard.py remain the
behavioural safety net for each hook's own decisions.
"""
import io
import os
import signal
import unittest

import hook_io

# Kill switch at MODULE scope, exactly like test_grill_pause / test_narrative_guard
# / test_tdd_hooks: `unittest discover -s hooks` runs this module SECOND of seven
# in one process, and the later suites spawn the real hooks as subprocesses that
# inherit os.environ. Setting it per-test and popping it in cleanup un-protected
# every suite after this one (merge-gate finding, reproduced) — tearDownModule
# below is the guard that keeps it that way.
os.environ["HOOK_JOURNAL_DISABLED"] = "1"


def tearDownModule():
    # The kill switch must OUTLIVE this module. If it does not, every simulated
    # Stop/PreToolUse event in the sibling suites appends to the operator's real
    # journal — which self-rotates at a byte cap and can push real hook history
    # out of both the live file and its single backup.
    assert os.environ.get("HOOK_JOURNAL_DISABLED") == "1", (
        "test_hook_io left HOOK_JOURNAL_DISABLED unset — later hook suites "
        "would write to the operator's real journal")


class TestReadPayload(unittest.TestCase):
    def _with_stdin(self, text):
        real = hook_io.sys.stdin
        hook_io.sys.stdin = io.StringIO(text)
        self.addCleanup(setattr, hook_io.sys, "stdin", real)
        return hook_io.read_payload()

    def test_parses_json_object(self):
        self.assertEqual(self._with_stdin('{"cwd": "/x"}'), {"cwd": "/x"})

    def test_empty_stdin_degrades_to_empty_dict(self):
        self.assertEqual(self._with_stdin(""), {})

    def test_torn_json_degrades_to_empty_dict(self):
        self.assertEqual(self._with_stdin('{"cwd": "/x'), {})

    def test_raising_read_cancels_the_alarm(self):
        # signal.alarm(0) only ran on the success path, so a read() that raises
        # anything but the alarm's own TimeoutError (UnicodeDecodeError on a
        # non-UTF-8 payload; OSError on an odd fd) returned {} with the 5s alarm
        # STILL PENDING — it then fired inside the hook's real work, e.g. mid
        # tdd_verify oracle run, crashing a Stop hook that fails OPEN.
        class BadStdin:
            def read(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

        real = hook_io.sys.stdin
        hook_io.sys.stdin = BadStdin()
        self.addCleanup(setattr, hook_io.sys, "stdin", real)
        self.addCleanup(signal.alarm, 0)          # never leak an alarm from a test
        self.assertEqual(hook_io.read_payload(), {})
        # alarm(0) both cancels and returns the seconds that were left pending.
        self.assertEqual(signal.alarm(0), 0)


class TestPayloadAccessors(unittest.TestCase):
    def test_session_id_present(self):
        self.assertEqual(hook_io.session_id({"session_id": "abc"}), "abc")

    def test_session_id_absent_or_empty_is_default(self):
        self.assertEqual(hook_io.session_id({}), "default")
        self.assertEqual(hook_io.session_id({"session_id": ""}), "default")

    def test_cwd_present(self):
        self.assertEqual(hook_io.cwd({"cwd": "/repo"}), "/repo")

    def test_cwd_absent_falls_back_to_process_cwd(self):
        import os
        self.assertEqual(hook_io.cwd({}), os.getcwd())

    def test_is_reentrant_stop_only_on_literal_true(self):
        self.assertTrue(hook_io.is_reentrant_stop({"stop_hook_active": True}))
        self.assertFalse(hook_io.is_reentrant_stop({"stop_hook_active": "yes"}))
        self.assertFalse(hook_io.is_reentrant_stop({}))


class TestJournalFor(unittest.TestCase):
    def test_returns_two_callables_that_never_raise(self):
        # The kill switch is set at MODULE scope (see top) and deliberately NOT
        # cleaned up — popping it here un-protected every later hook suite.
        self.assertEqual(os.environ.get("HOOK_JOURNAL_DISABLED"), "1")
        journal, drift = hook_io.journal_for("test-hook-io")
        self.assertTrue(callable(journal) and callable(drift))
        # Whatever backs them (real journal or no-op fallback), calling with
        # arbitrary args must not raise — hooks rely on this to stay fail-open.
        journal("skip", "unit-test", {"session_id": "t"})
        drift({"session_id": "t"}, ("session_id",))


if __name__ == "__main__":
    unittest.main()
