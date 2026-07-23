#!/usr/bin/env python3
"""Tests for hook_io — the shared hook-protocol I/O module.

These pin the protocol contract ONCE (stdin read, torn-JSON fallback, session
and cwd resolution, Stop loop-guard, guarded journal handles); the per-hook
subprocess suites in test_tdd_hooks.py / test_narrative_guard.py remain the
behavioural safety net for each hook's own decisions.
"""
import io
import unittest

import hook_io


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
        import os
        os.environ["HOOK_JOURNAL_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "HOOK_JOURNAL_DISABLED", None)
        journal, drift = hook_io.journal_for("test-hook-io")
        self.assertTrue(callable(journal) and callable(drift))
        # Whatever backs them (real journal or no-op fallback), calling with
        # arbitrary args must not raise — hooks rely on this to stay fail-open.
        journal("skip", "unit-test", {"session_id": "t"})
        drift({"session_id": "t"}, ("session_id",))


if __name__ == "__main__":
    unittest.main()
