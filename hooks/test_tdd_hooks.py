#!/usr/bin/env python3
"""Verification suite for the 4 TDD hooks (tdd_keyword/guard/mark/verify).

Stdlib `unittest` only — pytest is not installed in this environment.
Run:  python3 hooks/test_tdd_hooks.py -v

Isolation: every test uses a synthetic `VERIFY-<uuid>` session id and cleans
up its own state files. `setUpModule`/`tearDownModule` assert that no real
(non-`VERIFY-`) session file under .tdd-state / .tdd-markers was changed.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".claude" / "hooks" / ".tdd-state"
MARKER_DIR = Path.home() / ".claude" / "hooks" / ".tdd-markers"


def run_hook(script, payload, cwd=None):
    """Invoke a hook script as a subprocess with `payload` piped to stdin."""
    return subprocess.run(
        ["python3", str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True, text=True, cwd=cwd, timeout=60,
    )


_SNAPSHOT = {}


def _names(d):
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def setUpModule():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    _SNAPSHOT["state"] = [n for n in _names(STATE_DIR) if "VERIFY-" not in n]
    _SNAPSHOT["marker"] = [n for n in _names(MARKER_DIR) if "VERIFY-" not in n]


def tearDownModule():
    # Safety gate: no real session file may have been added/removed/clobbered.
    state_after = [n for n in _names(STATE_DIR) if "VERIFY-" not in n]
    marker_after = [n for n in _names(MARKER_DIR) if "VERIFY-" not in n]
    assert state_after == _SNAPSHOT["state"], (
        "real .tdd-state changed!", _SNAPSHOT["state"], state_after)
    assert marker_after == _SNAPSHOT["marker"], (
        "real .tdd-markers changed!", _SNAPSHOT["marker"], marker_after)
    leftover = ([n for n in _names(STATE_DIR) if "VERIFY-" in n]
                + [n for n in _names(MARKER_DIR) if "VERIFY-" in n])
    assert not leftover, ("VERIFY- state files were not cleaned up", leftover)


class HookTest(unittest.TestCase):
    """Base: fresh synthetic session id per test + guaranteed cleanup."""

    def setUp(self):
        self.sid = "VERIFY-" + uuid.uuid4().hex
        self._tmps = []

    def tearDown(self):
        for p in (self.mode_file(), self.edits_file(), self.marker_file()):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        for t in self._tmps:
            t.cleanup()

    # --- paths ---
    def mode_file(self):
        return STATE_DIR / f"mode-{self.sid}"

    def edits_file(self):
        return STATE_DIR / f"edits-{self.sid}.json"

    def marker_file(self):
        return MARKER_DIR / f"marker-{self.sid}"

    # --- helpers ---
    def tdd_on(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.mode_file().write_text(str(time.time()))

    def set_edits(self, **kw):
        self.edits_file().write_text(json.dumps(kw))

    def write_marker(self, path="/some/changed/file.py"):
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        self.marker_file().write_text(path)

    def tmpdir(self):
        t = tempfile.TemporaryDirectory()
        self._tmps.append(t)
        return Path(t.name)

    def project(self, test_cmd):
        """A temp project dir carrying a `.claude/tdd-test-cmd` override."""
        d = self.tmpdir()
        cdir = d / ".claude"
        cdir.mkdir()
        (cdir / "tdd-test-cmd").write_text(test_cmd + "\n")
        return d


# --------------------------------------------------------------------------
# tdd_keyword.py  (UserPromptSubmit)
# --------------------------------------------------------------------------
class TestKeywordHook(HookTest):

    def test_on_english(self):
        for prompt in ["use tdd for this", "test-driven development please",
                       "let's go test first", "do a red-green cycle"]:
            with self.subTest(prompt=prompt):
                self.mode_file().unlink(missing_ok=True)
                r = run_hook("tdd_keyword.py",
                             {"prompt": prompt, "session_id": self.sid})
                self.assertEqual(r.returncode, 0)
                self.assertIn("[TDD MODE]", r.stdout)
                self.assertTrue(self.mode_file().exists())

    def test_on_korean(self):
        for prompt in ["테스트 주도로 짜줘", "테스트 먼저 작성해줘",
                       "테스트 우선으로 가자", "테스트부터 만들어",
                       "레드 그린 사이클로"]:
            with self.subTest(prompt=prompt):
                self.mode_file().unlink(missing_ok=True)
                r = run_hook("tdd_keyword.py",
                             {"prompt": prompt, "session_id": self.sid})
                self.assertEqual(r.returncode, 0)
                self.assertIn("[TDD MODE]", r.stdout)
                self.assertTrue(self.mode_file().exists())

    def test_off(self):
        for prompt in ["tdd off", "tdd 종료", "tdd 모드 해제"]:
            with self.subTest(prompt=prompt):
                self.tdd_on()
                r = run_hook("tdd_keyword.py",
                             {"prompt": prompt, "session_id": self.sid})
                self.assertEqual(r.returncode, 0)
                self.assertIn("[TDD MODE OFF]", r.stdout)
                self.assertFalse(self.mode_file().exists())

    def test_sticky(self):
        r1 = run_hook("tdd_keyword.py",
                      {"prompt": "use tdd", "session_id": self.sid})
        self.assertIn("[TDD MODE]", r1.stdout)
        # follow-up prompt with no TDD keyword still re-injects the pointer
        r2 = run_hook("tdd_keyword.py",
                      {"prompt": "now add a logout button", "session_id": self.sid})
        self.assertIn("[TDD MODE]", r2.stdout)

    def test_bare_test_no_trigger(self):
        for prompt in ["테스트해줘", "run the tests please", "just test it"]:
            with self.subTest(prompt=prompt):
                r = run_hook("tdd_keyword.py",
                             {"prompt": prompt, "session_id": self.sid})
                self.assertEqual(r.returncode, 0)
                self.assertNotIn("[TDD MODE]", r.stdout)
                self.assertFalse(self.mode_file().exists())


# --------------------------------------------------------------------------
# tdd_guard.py  (PreToolUse Edit|Write)
# --------------------------------------------------------------------------
class TestGuardHook(HookTest):

    def _guard(self, tool, file_path):
        return run_hook("tdd_guard.py", {
            "session_id": self.sid, "tool_name": tool,
            "tool_input": {"file_path": str(file_path)},
        })

    def test_noop_when_off(self):
        r = self._guard("Write", self.tmpdir() / "new.py")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr.strip(), "")

    def test_block_new_impl_no_test(self):
        self.tdd_on()
        r = self._guard("Write", self.tmpdir() / "feature.py")  # not on disk
        self.assertEqual(r.returncode, 2)
        self.assertIn("blocked: creating a new implementation file", r.stderr)

    def test_warn_existing_impl_no_test(self):
        self.tdd_on()
        existing = self.tmpdir() / "feature.py"
        existing.write_text("# already here\n")
        r = self._guard("Edit", existing)
        self.assertEqual(r.returncode, 0)
        self.assertIn("[TDD MODE] You are editing an implementation file",
                      r.stdout)

    def test_allow_test_file(self):
        self.tdd_on()
        d = self.tmpdir()
        for rel in ["test_x.py", "x.spec.ts", "tests/x.py"]:
            with self.subTest(path=rel):
                r = self._guard("Write", d / rel)
                self.assertEqual(r.returncode, 0)
                self.assertNotIn("blocked", r.stderr)
                self.assertNotIn("[TDD MODE]", r.stdout)

    def test_allow_after_test_edited(self):
        self.tdd_on()
        self.set_edits(last_test=time.time())
        r = self._guard("Write", self.tmpdir() / "feature.py")
        self.assertEqual(r.returncode, 0)

    def test_allow_non_code_ext(self):
        self.tdd_on()
        d = self.tmpdir()
        for rel in ["README.md", "config.yaml"]:
            with self.subTest(path=rel):
                r = self._guard("Write", d / rel)
                self.assertEqual(r.returncode, 0)


# --------------------------------------------------------------------------
# tdd_mark.py  (PostToolUse Edit|Write)
# --------------------------------------------------------------------------
class TestMarkHook(HookTest):

    def _mark(self, file_path):
        return run_hook("tdd_mark.py", {
            "session_id": self.sid,
            "tool_input": {"file_path": str(file_path)},
        })

    def test_writes_marker(self):
        r = self._mark("/repo/src/foo.py")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(self.marker_file().exists())
        self.assertIn("/repo/src/foo.py", self.marker_file().read_text().splitlines())

    def test_marker_accumulates_distinct_paths(self):
        # The marker accumulates the DISTINCT edited paths this turn (deduped),
        # so the Stop verifier can resolve every repo touched — not just the last.
        self._mark("/repo/a.py")
        self._mark("/repo/b.py")
        self._mark("/repo/a.py")   # duplicate -> not re-added
        self.assertEqual(self.marker_file().read_text().splitlines(),
                         ["/repo/a.py", "/repo/b.py"])

    def test_ignores_non_code(self):
        r = self._mark("/repo/notes.md")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.marker_file().exists())

    def test_records_last_test_when_on(self):
        self.tdd_on()
        self._mark("/repo/tests/test_foo.py")
        data = json.loads(self.edits_file().read_text())
        self.assertGreater(data.get("last_test", 0), 0)
        self.assertNotIn("last_impl", data)

    def test_records_last_impl_when_on(self):
        self.tdd_on()
        self._mark("/repo/src/foo.py")
        data = json.loads(self.edits_file().read_text())
        self.assertGreater(data.get("last_impl", 0), 0)

    def test_no_edits_when_off(self):
        self._mark("/repo/src/foo.py")  # no mode file
        self.assertTrue(self.marker_file().exists())
        self.assertFalse(self.edits_file().exists())


# --------------------------------------------------------------------------
# tdd_verify.py  (Stop)
# --------------------------------------------------------------------------
class TestVerifyHook(HookTest):

    def _verify(self, cwd, **extra):
        payload = {"session_id": self.sid, "cwd": str(cwd)}
        payload.update(extra)
        return run_hook("tdd_verify.py", payload)

    def test_noop_no_marker(self):
        r = self._verify(self.tmpdir())  # no marker for this turn
        self.assertEqual(r.returncode, 0)

    def test_loop_guard(self):
        d = self.project("exit 1")  # would fail if it ran
        self.write_marker(str(d / "src.py"))
        r = self._verify(d, stop_hook_active=True)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(self.marker_file().exists())  # not consumed

    def test_green_pass(self):
        d = self.project("exit 0")
        self.write_marker(str(d / "src.py"))
        r = self._verify(self.tmpdir())  # cwd is irrelevant; the edited repo drives
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.marker_file().exists())  # consumed

    def test_red_block(self):
        d = self.project("echo FAILMARK; exit 1")
        self.write_marker(str(d / "src.py"))
        r = self._verify(self.tmpdir())
        self.assertEqual(r.returncode, 2)
        self.assertIn("Tests are NOT green", r.stderr)
        self.assertIn("FAILMARK", r.stderr)

    def test_pytest_exit5_nonblocking(self):
        # cmd string contains the token "pytest" -> exit 5 treated as no-op
        d = self.project("sh -c 'exit 5'  # pytest")
        self.write_marker(str(d / "src.py"))
        r = self._verify(self.tmpdir())
        self.assertEqual(r.returncode, 0)

    def test_unknown_project(self):
        d = self.tmpdir()  # bare dir, no project markers
        self.write_marker(str(d / "src.py"))
        r = self._verify(self.tmpdir())
        self.assertEqual(r.returncode, 0)

    def test_venue_resolves_edited_repo_not_session_cwd(self):
        # ADR-0023 / ADR-0022 §8: the oracle runs against the repo whose files
        # CHANGED this turn (per the marker), NOT the session cwd. Under the
        # operator's plan-repo-session / code-repo-edit workflow these differ,
        # and cwd resolves the wrong repo. Edited repo RED + session-cwd repo
        # GREEN -> must BLOCK on the edited repo (not pass on cwd's green).
        edited = self.project("echo EDITEDRED; exit 1")
        cwd_repo = self.project("exit 0")
        self.write_marker(str(edited / "src.py"))
        r = self._verify(cwd_repo)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("EDITEDRED", r.stderr)

    def test_multi_repo_runs_each_edited_oracle(self):
        # Multiple repos edited in one turn -> the verifier runs EACH repo's
        # oracle; a red in ANY blocks. repoA green, repoB red -> block on B.
        a = self.project("exit 0")
        b = self.project("echo BRED; exit 1")
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        self.marker_file().write_text(f"{a / 'a.py'}\n{b / 'b.py'}\n")
        r = self._verify(self.tmpdir())
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BRED", r.stderr)

    def test_empty_marker_degrades_to_session_cwd(self):
        # ADR-0023 back-compat: a present-but-empty/whitespace marker (no usable
        # paths) degrades to the SESSION CWD's oracle, so the verifier is never
        # silently disabled by a contentless marker.
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        self.marker_file().write_text("   \n\n")   # whitespace only -> no paths
        d = self.project("echo CWDRAN; exit 1")
        r = self._verify(d)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("CWDRAN", r.stderr)

    def test_hanging_suite_times_out_as_nonblocking_skip(self):
        # tdd_verify must never freeze the turn on a hung suite: a bounded
        # per-suite timeout kills the process group and treats the timeout as a
        # non-blocking infra skip (Stop-time private feedback, not a red verdict).
        # The oracle here would BLOCK (exit 1) if it ran to completion; the
        # timeout (shortened via TDD_ORACLE_TIMEOUT_SECONDS) must pre-empt it.
        d = self.project("sleep 10; exit 1")
        self.write_marker(str(d / "src.py"))
        start = time.time()
        r = subprocess.run(
            ["python3", str(HOOKS / "tdd_verify.py")],
            input=json.dumps({"session_id": self.sid, "cwd": str(self.tmpdir())}),
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "TDD_ORACLE_TIMEOUT_SECONDS": "1"},
        )
        elapsed = time.time() - start
        self.assertEqual(r.returncode, 0, r.stderr)   # infra skip, NOT a block
        self.assertLess(elapsed, 6, "suite was not killed promptly — hung past the timeout")

    @unittest.skipUnless(shutil.which("npm"), "npm not installed")
    def test_npm_real_detection(self):
        d = self.tmpdir()
        (d / "package.json").write_text(json.dumps(
            {"name": "v", "version": "0.0.0", "scripts": {"test": "exit 0"}}))
        self.write_marker(str(d / "index.js"))
        r = self._verify(self.tmpdir())
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.marker_file().exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
