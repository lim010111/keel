#!/usr/bin/env python3
"""Tests for the merge-gate scheduler hook (claude-harness-work #30).

Stdlib unittest only. Run: python3 hooks/test_merge_gate_hooks.py -v

Covers the cheap/diff gate decision logic (pure) and the Stop scheduler's launch
wiring (in-process with Popen patched — never spawns a real produce).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
SCRIPTS = HOOKS.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HOOKS))

import merge_gate_local as mg          # noqa: E402
import merge_gate_scheduler as mgs     # noqa: E402


def init_repo(path: Path, profile="local", reviewers='["codex"]', extra=""):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    toml = ""
    if profile is not None:
        toml = ("[merge-gate]\n"
                f'profile = "{profile}"\n\n'
                "[merge-gate.local.producer]\n"
                f"reviewers = {reviewers}\n" + extra)
    if toml:
        (path / "harness.toml").write_text(toml)
    (path / "base.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True, env=env)
    return env


# --------------------------------------------------------------------------
# pure gate logic
# --------------------------------------------------------------------------
class TestGates(unittest.TestCase):
    def _cfg(self, **sched):
        d = mg._merge_defaults(mg.DEFAULT_CONFIG, {})
        d["scheduler"] = {**d["scheduler"], **sched}
        return mg.Config(d)

    def test_auto_produce_off(self):
        st = {"dirty": True, "last_edit_ts": 0, "last_produce_ts": 0}
        ok, _ = mgs.cheap_gate(st, self._cfg(auto_produce="off"), time.time(), False)
        self.assertFalse(ok)

    def test_recursion_guard(self):
        st = {"dirty": True, "last_edit_ts": 0, "last_produce_ts": 0}
        ok, why = mgs.cheap_gate(st, self._cfg(), time.time(), True)
        self.assertFalse(ok)
        self.assertIn("recursion", why)

    def test_not_dirty(self):
        st = {"dirty": False}
        ok, _ = mgs.cheap_gate(st, self._cfg(), time.time(), False)
        self.assertFalse(ok)

    def test_debounce(self):
        now = time.time()
        st = {"dirty": True, "last_edit_ts": now, "last_produce_ts": 0}
        ok, why = mgs.cheap_gate(st, self._cfg(debounce_seconds=90), now, False)
        self.assertFalse(ok)
        self.assertIn("debounc", why)

    def test_min_interval(self):
        now = time.time()
        st = {"dirty": True, "last_edit_ts": now - 1000, "last_produce_ts": now - 10}
        ok, why = mgs.cheap_gate(st, self._cfg(min_interval_seconds=600), now, False)
        self.assertFalse(ok)
        self.assertIn("min-interval", why)

    def test_cheap_pass(self):
        now = time.time()
        st = {"dirty": True, "last_edit_ts": now - 1000, "last_produce_ts": now - 1000}
        ok, _ = mgs.cheap_gate(st, self._cfg(), now, False)
        self.assertTrue(ok)

    def test_diff_gate_no_changes(self):
        ok, _ = mgs.diff_gate({}, {"changed_files": [], "diff_hash": "x"}, False)
        self.assertFalse(ok)

    def test_diff_gate_same_diff(self):
        ok, why = mgs.diff_gate({"last_diff_hash": "h"},
                                {"changed_files": ["a"], "diff_hash": "h"}, False)
        self.assertFalse(ok)
        self.assertIn("same diff", why)

    def test_diff_gate_lock_held(self):
        ok, why = mgs.diff_gate({"last_diff_hash": "old"},
                                {"changed_files": ["a"], "diff_hash": "h"}, True)
        self.assertFalse(ok)
        self.assertIn("lock", why)

    def test_diff_gate_produce(self):
        ok, _ = mgs.diff_gate({"last_diff_hash": "old"},
                              {"changed_files": ["a"], "diff_hash": "h"}, False)
        self.assertTrue(ok)


# --------------------------------------------------------------------------
# scheduler main() — launch wiring (Popen patched; never spawns produce)
# --------------------------------------------------------------------------
class TestSchedulerMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env = init_repo(self.root)
        self._state = tempfile.TemporaryDirectory()
        self._orig_state_root = mgs.STATE_ROOT
        mgs.STATE_ROOT = Path(self._state.name)
        self._orig_launch = mgs.launch_produce
        self.launched = []
        mgs.launch_produce = lambda root, sdir: self.launched.append((root, sdir))
        self._orig_read = mgs.read_input

    def tearDown(self):
        mgs.STATE_ROOT = self._orig_state_root
        mgs.launch_produce = self._orig_launch
        mgs.read_input = self._orig_read
        self._tmp.cleanup()
        self._state.cleanup()

    def _run(self, payload, env_extra=None):
        mgs.read_input = lambda: payload
        old = dict(os.environ)
        if env_extra:
            os.environ.update(env_extra)
        try:
            with self.assertRaises(SystemExit) as cm:
                mgs.main()
            return cm.exception.code
        finally:
            os.environ.clear()
            os.environ.update(old)

    def _seed_state(self, **over):
        sdir = mgs.repo_state_dir(self.root)
        st = {"dirty": True, "last_edit_ts": 0, "last_produce_ts": 0}
        st.update(over)
        mgs.save_state(sdir, st)
        return sdir

    def test_self_gate_non_local(self):
        # reinit with no merge-gate profile
        import shutil
        shutil.rmtree(self.root)
        self.root.mkdir()
        init_repo(self.root, profile=None)
        self._seed_state()
        self._run({"cwd": str(self.root)})
        self.assertEqual(self.launched, [])

    def test_not_dirty_no_launch(self):
        self._seed_state(dirty=False)
        self._run({"cwd": str(self.root)})
        self.assertEqual(self.launched, [])

    def test_recursion_guard_no_launch(self):
        self._seed_state()
        # make an in-scope change so only the guard stops it
        (self.root / "new.py").write_text("x=1\n")
        self._run({"cwd": str(self.root)}, env_extra={"MERGE_GATE_PRODUCER_RUNNING": "1"})
        self.assertEqual(self.launched, [])

    def test_stop_hook_active_no_launch(self):
        self._seed_state()
        (self.root / "new.py").write_text("x=1\n")
        self._run({"cwd": str(self.root), "stop_hook_active": True})
        self.assertEqual(self.launched, [])

    def test_launches_and_updates_state(self):
        self._seed_state()
        (self.root / "new.py").write_text("x=1\n")  # in-scope diff
        code = self._run({"cwd": str(self.root)})
        self.assertEqual(code, 0)
        self.assertEqual(len(self.launched), 1)
        launched_root, _ = self.launched[0]
        self.assertEqual(Path(launched_root), self.root)
        # state updated: dirty cleared, dedup key recorded
        st = mgs.load_state(mgs.repo_state_dir(self.root))
        self.assertFalse(st["dirty"])
        self.assertTrue(st["last_diff_hash"])

    def test_no_inscope_diff_no_launch(self):
        # dirty but only an ignored-path change
        self._seed_state()
        (self.root / ".merge-gate").mkdir(exist_ok=True)
        (self.root / ".merge-gate" / "junk").write_text("z\n")
        self._run({"cwd": str(self.root)})
        self.assertEqual(self.launched, [])


if __name__ == "__main__":
    unittest.main()
