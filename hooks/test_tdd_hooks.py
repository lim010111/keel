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
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".claude" / "hooks" / ".tdd-state"
MARKER_DIR = Path.home() / ".claude" / "hooks" / ".tdd-markers"

# The hook under test. find_up / detect_test_command are pure resolution
# functions (no stdin/exit-code semantics), so the bounding logic is unit-tested
# by importing them directly rather than through the subprocess wrapper.
sys.path.insert(0, str(HOOKS))
import tdd_verify  # noqa: E402


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
        # The pytest probe is memoized per process; clear it so each test starts
        # from a clean cache (the hook itself runs as a fresh process per Stop).
        for fn in (getattr(tdd_verify, "_pytest_importable", None),
                   getattr(tdd_verify, "pytest_available", None)):
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
        # work-interval-tdd#08: reset the per-process transient-retry latch so each
        # test starts fresh (the hook itself runs as a new process per Stop).
        tdd_verify._pytest_retry_used = False

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

    def inner_repo(self, parent, name="inner", git="dir"):
        """A child dir of `parent` that is its OWN repo (carries `.git`) with no
        oracle marker — the inner-repo `R` of work-interval-tdd#01. `git="dir"`
        makes `.git` a directory; `git="file"` makes it a regular file (the
        worktree / submodule shape)."""
        r = parent / name
        r.mkdir(parents=True)
        if git == "file":
            (r / ".git").write_text("gitdir: /elsewhere/worktree\n")
        else:
            (r / ".git").mkdir()
        return r


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

    def test_ancestor_override_not_resolved_across_repo_boundary(self):
        # work-interval-tdd#01 A2: the priority-1 `.claude/tdd-test-cmd` override
        # of an ANCESTOR repo must NOT be resolved for an edit inside an inner
        # repo R that has its own `.git` and no marker. A trusted, arbitrary
        # command pulled from an unrelated parent is the worst escape — resolution
        # is bounded to R's own repo boundary, so nothing runs.
        ancestor = self.project("echo ANCESTOR; exit 1")
        r = self.inner_repo(ancestor)              # R/.git dir, no marker
        self.write_marker(str(r / "src.py"))
        res = self._verify(self.tmpdir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("ANCESTOR", res.stderr)

    def test_degrade_to_cwd_is_bounded_at_cwd_git(self):
        # work-interval-tdd#01 A6: a present-but-empty marker degrades to the
        # session cwd (ADR-0023), and THAT cwd resolution is itself bounded at
        # cwd's own `.git`. cwd = inner repo R (own .git, no marker) under an
        # ancestor override -> the ancestor's command must NOT be pulled in via
        # the degrade path. (Distinct from test_empty_marker_degrades_to_session_cwd,
        # which only proves the degrade fires; this proves it stays bounded.)
        ancestor = self.project("echo ANCESTOR; exit 1")
        r = self.inner_repo(ancestor)              # R/.git dir, no marker
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        self.marker_file().write_text("   \n\n")   # empty -> degrade to cwd
        res = self._verify(r)                      # cwd = inner repo R
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("ANCESTOR", res.stderr)

    def test_suppressed_ancestor_marker_emits_diagnostic(self):
        # work-interval-tdd#07 A1: an edit in inner repo R (own .git, no marker)
        # under an ancestor that DOES carry an override marker emits a SINGLE
        # stderr diagnostic naming the bounded repo R and the suppressed ancestor
        # marker dir — the #01 bound is now observable, not silent. Enforcement is
        # unchanged: the ancestor's suite is NOT run (returncode 0, no ANCESTOR).
        ancestor = self.project("echo ANCESTOR; exit 1")
        r = self.inner_repo(ancestor)              # R/.git dir, no marker
        self.write_marker(str(r / "src.py"))
        res = self._verify(self.tmpdir())
        self.assertEqual(res.returncode, 0, res.stderr)   # enforcement unchanged
        self.assertNotIn("ANCESTOR", res.stderr)          # ancestor suite NOT run
        self.assertIn("enforcement suppressed", res.stderr)
        self.assertIn(str(r.resolve()), res.stderr)         # names the bounded repo R
        self.assertIn(str(ancestor.resolve()), res.stderr)  # names the ancestor dir
        self.assertEqual(res.stderr.count("enforcement suppressed"), 1)  # single

    def test_no_oracle_anywhere_stays_silent(self):
        # work-interval-tdd#07 A2: the diagnostic fires ONLY on a suppressed
        # ancestor marker. The ordinary ADR-0024 "no oracle anywhere" skip — a
        # git-less, marker-less tree — stays silent: no new noise for the
        # silent-skip classes the ADR deliberately keeps quiet.
        d = self.tmpdir()                       # no markers, no .git, no override
        sub = d / "a" / "b"
        sub.mkdir(parents=True)
        self.write_marker(str(sub / "src.py"))
        res = self._verify(self.tmpdir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("enforcement suppressed", res.stderr)

    def test_pytest_available_is_cached(self):
        # work-interval-tdd#07 (merge-gate claude:finding-0): the skip-path probe
        # re-runs detect_test_command on the Stop hot path, which can call
        # pytest_available() repeatedly. It spawns a `python3 -c import pytest`
        # subprocess, so it must be memoized per process — not re-spawned per call.
        # (setUp already cleared the probe cache.)
        real_run = subprocess.run
        calls = []
        def counting_run(*a, **k):
            calls.append(a)
            return real_run(*a, **k)
        with mock.patch.object(tdd_verify.subprocess, "run", side_effect=counting_run):
            r1 = tdd_verify.pytest_available()
            r2 = tdd_verify.pytest_available()
            r3 = tdd_verify.pytest_available()
        self.assertEqual([r1, r2, r3], [r1, r1, r1])     # stable result
        self.assertEqual(len(calls), 1,
                         "pytest_available spawned a subprocess on every call (uncached)")

    def test_pytest_available_does_not_cache_transient_failure(self):
        # work-interval-tdd#07 (merge-gate re-review, low/unsure): a TRANSIENT probe
        # failure (subprocess timeout/exception — most likely under load on the Stop
        # hot path) must NOT be memoized. Otherwise one flaky 15s timeout fail-opens
        # TDD verification for EVERY Python repo for the rest of the turn. A
        # definitive result is cached; a transient failure re-probes on the next call.
        # (#08 refines this: the FIRST call now does one bounded inline retry on a
        # transient failure. Both probes fail here, so the call still falls open to
        # False; the property under test — a transient result is never MEMOIZED — is
        # proven by the next call re-probing to True.)
        with mock.patch.object(
                tdd_verify.subprocess, "run",
                side_effect=[subprocess.TimeoutExpired(cmd="x", timeout=15),
                             subprocess.TimeoutExpired(cmd="x", timeout=15),
                             mock.Mock(returncode=0)]):
            first = tdd_verify.pytest_available()    # transient timeout (x2) -> False
            second = tdd_verify.pytest_available()   # must re-probe -> True
        self.assertFalse(first)
        self.assertTrue(second, "transient failure was cached — re-probe did not run")

    def test_pytest_available_does_not_cache_signal_kill(self):
        # work-interval-tdd#07 (merge-gate round-3, medium/upheld): a probe that
        # COMPLETES with a signal-kill exit (OOM -9, segfault -11) is abnormal /
        # transient — the failure mode most correlated with the high-load condition
        # that motivates caching — not a definitive import result. It completes
        # WITHOUT raising, so it must be explicitly treated as transient: not
        # memoized, re-probed on the next call (mirrors the timeout/OSError path).
        # (#08: the first call retries the transient probe inline; both are killed
        # here, so it still falls open to False, and the next call re-probes to True
        # — proving the signal-killed result was not memoized.)
        with mock.patch.object(
                tdd_verify.subprocess, "run",
                side_effect=[mock.Mock(returncode=-9),    # killed by SIGKILL (OOM)
                             mock.Mock(returncode=-9),    # inline retry also killed
                             mock.Mock(returncode=0)]):    # re-probe succeeds
            first = tdd_verify.pytest_available()
            second = tdd_verify.pytest_available()
        self.assertFalse(first)
        self.assertTrue(second, "signal-killed probe was cached — re-probe did not run")

    def test_transient_probe_retries_then_resolves_suite(self):
        # work-interval-tdd#08 A1: a single-file Python edit whose FIRST pytest
        # probe fails transiently (signal-kill / timeout) but whose retry would
        # succeed must RESOLVE the suite, not silently skip. #07 isolated the
        # transient signal (the raise path); #08 does ONE bounded inline retry on
        # it before the ADR-0024 fail-open, so the edited repo's oracle still runs
        # that turn. Asserted at the public interface (detect_test_command resolving
        # a runnable command), not on lru_cache internals.
        proj = self.tmpdir()
        (proj / "pyproject.toml").write_text("")
        src = proj / "pkg"; src.mkdir()
        with mock.patch.object(
                tdd_verify.subprocess, "run",
                side_effect=[mock.Mock(returncode=-9),    # first probe: SIGKILL (transient)
                             mock.Mock(returncode=0)]):    # retry: pytest importable
            root, cmd = tdd_verify.detect_test_command(str(src))
        self.assertEqual(root, proj.resolve(),
                         "transient first probe silently skipped the suite — no retry")
        self.assertEqual(cmd, "python3 -m pytest -q")

    def test_transient_retry_is_bounded_per_process(self):
        # work-interval-tdd#08 A2: the retry budget is per PROCESS, not per call.
        # pytest_available() runs several times per turn (once per edited dir + the
        # skip-path probe), so a naive per-call retry would re-introduce the
        # N-subprocess churn #07 removed (merge-gate claude:finding-0). Under
        # sustained transient failure the WHOLE process gets at most ONE extra
        # probe: the first call probes twice (initial + the single retry), every
        # later call probes once (latch consumed). 3 calls => 4 probes, not 6.
        calls = []
        def always_killed(*a, **k):
            calls.append(a)
            return mock.Mock(returncode=-9)    # every probe SIGKILLed (transient)
        with mock.patch.object(tdd_verify.subprocess, "run", side_effect=always_killed):
            results = [tdd_verify.pytest_available() for _ in range(3)]
        self.assertEqual(results, [False, False, False])   # always falls open
        self.assertEqual(len(calls), 4,
                         "transient retry not bounded to one extra probe per process")

    def test_definitive_absent_does_not_retry(self):
        # work-interval-tdd#08 A2: only the TRANSIENT signal (the raise path) is
        # eligible for the retry. A DEFINITIVE "pytest absent" (returncode > 0,
        # ModuleNotFoundError) is not transient — it must skip silently with NO
        # retry and be memoized, exactly per ADR-0024 (this refines, not reverses,
        # the fail-open philosophy). Two calls => one probe total (cached, no retry).
        calls = []
        def absent(*a, **k):
            calls.append(a)
            return mock.Mock(returncode=1)     # genuine ModuleNotFoundError
        with mock.patch.object(tdd_verify.subprocess, "run", side_effect=absent):
            first = tdd_verify.pytest_available()
            second = tdd_verify.pytest_available()
        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(len(calls), 1,
                         "definitive 'pytest absent' triggered a retry or re-probe")

    def test_suppressed_diagnostic_dedups_per_repo(self):
        # work-interval-tdd#07 A1 (review F5): the `suppressed` dict is keyed by
        # repo_root so MULTIPLE edited files in ONE inner repo collapse to ONE
        # diagnostic. Two files in the same bounded repo R (under a runnable
        # ancestor oracle) must emit exactly one line — a plain per-file list
        # would emit two. Locks the dedup the single-file A1 test cannot exercise.
        ancestor = self.project("exit 0")          # runnable ancestor override
        r = self.inner_repo(ancestor)              # R/.git dir, no marker
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        self.marker_file().write_text(f"{r / 'a.py'}\n{r / 'b.py'}\n")
        res = self._verify(self.tmpdir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr.count("enforcement suppressed"), 1)

    def test_runner_absent_ancestor_marker_stays_silent(self):
        # work-interval-tdd#07 A2 (review F1/F3): the diagnostic must distinguish
        # "an oracle WAS available and the #01 bound suppressed it" from "a marker
        # file merely exists above the bound." An ancestor package.json with NO
        # test script resolves to NO runnable oracle (detect_test_command returns
        # None), so it is an ADR-0024 silent-skip, NOT a suppression — even though
        # the marker file exists above the boundary. Host-independent: the
        # no-test-script rejection needs no installed runner.
        ancestor = self.tmpdir()
        (ancestor / "package.json").write_text('{"name":"x","version":"0.0.0"}')
        r = self.inner_repo(ancestor)              # R/.git dir, no marker
        self.write_marker(str(r / "src.py"))
        res = self._verify(self.tmpdir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("enforcement suppressed", res.stderr)

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


# --------------------------------------------------------------------------
# tdd_verify.find_up  (resolution bounding — work-interval-tdd#01)
# --------------------------------------------------------------------------
class TestFindUpBounding(HookTest):
    """find_up resolves an oracle only within the edited file's OWN repo —
    never an ancestor's. Unit-tests the bounded ascent directly."""

    def test_inner_repo_does_not_resolve_ancestor_marker(self):
        # A1: an edited file in inner repo R (own .git, no marker) resolves NO
        # suite — never the conventional marker living in the ancestor above R.
        ancestor = self.tmpdir()
        (ancestor / "pyproject.toml").write_text("")
        r = self.inner_repo(ancestor)          # R/.git dir, no marker
        src = r / "pkg"
        src.mkdir()
        d, name = tdd_verify.find_up(str(src), ["pyproject.toml", "package.json"])
        self.assertIsNone(d)
        self.assertIsNone(name)

    def test_git_file_bounds_worktree(self):
        # A4: git worktrees / submodules use a `.git` *file*, not a dir, so the
        # boundary check is `.exists()` not `.is_dir()` — a worktree edit is
        # bounded to its own worktree and never resolves the ancestor's marker.
        ancestor = self.tmpdir()
        (ancestor / "pyproject.toml").write_text("")
        r = self.inner_repo(ancestor, git="file")   # R/.git is a FILE
        src = r / "pkg"
        src.mkdir()
        d, _ = tdd_verify.find_up(str(src), ["pyproject.toml"])
        self.assertIsNone(d)

    def test_marker_colocated_with_git_root_still_resolves(self):
        # A5 (regression): a marker co-located with `.git` at the repo root — the
        # common single-repo case — still resolves, because the marker is checked
        # BEFORE the `.git` stop at each level. Stop-before-check would break it.
        root = self.tmpdir()
        (root / ".git").mkdir()
        (root / "pyproject.toml").write_text("")
        src = root / "pkg"
        src.mkdir()
        d, name = tdd_verify.find_up(str(src), ["pyproject.toml"])
        self.assertEqual(d, root.resolve())
        self.assertEqual(name, "pyproject.toml")

    def test_marker_colocated_with_git_FILE_root_still_resolves(self):
        # work-interval-tdd#07 A3: the worktree / submodule single-repo case — a
        # `.git` *file* root co-located with a marker — still resolves. This is the
        # union of #01's A4 (`.git`-file bounds) and A5 (`.git`-dir + marker
        # resolves): logically implied (marker checked before the `.exists()` stop,
        # which already counts a `.git` file) but previously untested, so the
        # marker-before-`.git`-stop ordering is now locked for the FILE case too.
        root = self.tmpdir()
        (root / ".git").write_text("gitdir: /elsewhere/worktree\n")  # .git FILE
        (root / "pyproject.toml").write_text("")
        src = root / "pkg"
        src.mkdir()
        d, name = tdd_verify.find_up(str(src), ["pyproject.toml"])
        self.assertEqual(d, root.resolve())
        self.assertEqual(name, "pyproject.toml")

    def test_non_git_project_resolves_via_ascent(self):
        # A3: the pre-existing fs-root cap is retained as the OUTER fallback — a
        # plain project under no git repo still resolves its marker by ascending.
        proj = self.tmpdir()
        (proj / "pyproject.toml").write_text("")
        sub = proj / "a" / "b"
        sub.mkdir(parents=True)                 # no .git anywhere in the tree
        d, name = tdd_verify.find_up(str(sub), ["pyproject.toml"])
        self.assertEqual(d, proj.resolve())
        self.assertEqual(name, "pyproject.toml")

    def test_home_is_exclusive_ceiling(self):
        # A7: $HOME is the universal ancestor of every path — an EXCLUSIVE
        # ceiling. A git-less edited file under $HOME must NOT resolve a marker
        # living directly at $HOME. The second assertion proves we closed the
        # CEILING, not the ascent: a marker BELOW $HOME still resolves (so the
        # None above is the ceiling firing, not merely the fs-root fallback).
        fake_home = self.tmpdir().resolve()
        sub = fake_home / "proj" / "sub"
        sub.mkdir(parents=True)                      # git-less path under $HOME
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            (fake_home / "pyproject.toml").write_text("")        # marker AT $HOME
            d_at_home, _ = tdd_verify.find_up(str(sub), ["pyproject.toml"])
            self.assertIsNone(d_at_home)

            (fake_home / "proj" / "pyproject.toml").write_text("")  # marker BELOW $HOME
            d_below, _ = tdd_verify.find_up(str(sub), ["pyproject.toml"])
            self.assertEqual(d_below, (fake_home / "proj").resolve())


# --------------------------------------------------------------------------
# tdd_verify._suppressed_ancestor  (skip-path probe — work-interval-tdd#07)
# --------------------------------------------------------------------------
class TestSuppressedAncestorProbe(HookTest):
    """The probe reports a suppressed ancestor oracle ONLY when LIFTING the #01
    bound WOULD resolve a *runnable* oracle strictly above the edited repo's
    `.git` boundary — and stays silent (None) for every ADR-0024 no-RUNNABLE-
    oracle class. It delegates to detect_test_command (stop_at_git=False), so a
    bare ancestor marker whose runner is absent is NOT a suppression — that
    existence-vs-runnability distinction is what satisfies #07 A2. Unit-tests the
    probe directly, parallel to TestFindUpBounding."""

    def _override(self, d, cmd="exit 0"):
        """Write a runnable `.claude/tdd-test-cmd` (returned verbatim, no runner
        check) into dir `d` — a host-independent stand-in for 'a runnable oracle'."""
        cdir = d / ".claude"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "tdd-test-cmd").write_text(cmd + "\n")
        return d

    def test_reports_suppressed_ancestor_override(self):
        # A1 core: a RUNNABLE ancestor oracle (override) above inner repo R (own
        # .git, no marker) is reported as suppressed — naming R and the ancestor.
        ancestor = self._override(self.tmpdir())            # ancestor/.claude/tdd-test-cmd
        r = self.inner_repo(ancestor)                       # R/.git dir, no marker
        src = r / "pkg"; src.mkdir()
        sup = tdd_verify._suppressed_ancestor(str(src))
        self.assertEqual(sup, (r.resolve(), ancestor.resolve()))

    def test_git_file_boundary_also_suppresses(self):
        # The boundary is `.exists()` (worktree/submodule `.git` FILE), so a
        # `.git`-file inner repo under a runnable ancestor oracle is suppressed too.
        ancestor = self._override(self.tmpdir())
        r = self.inner_repo(ancestor, git="file")           # R/.git is a FILE
        src = r / "pkg"; src.mkdir()
        sup = tdd_verify._suppressed_ancestor(str(src))
        self.assertEqual(sup, (r.resolve(), ancestor.resolve()))

    def test_override_priority_over_closer_conventional(self):
        # review F2: the probe inherits detect_test_command's override-BEFORE-
        # conventional priority regardless of depth. An override at a FARTHER
        # ancestor and a conventional marker at a NEARER one -> the probe names the
        # OVERRIDE dir (what would actually run), not the physically closer marker.
        outer = self._override(self.tmpdir())               # override at the FAR root
        mid = outer / "mid"; mid.mkdir()
        (mid / "pyproject.toml").write_text("")             # conventional, NEARER
        r = self.inner_repo(mid)                            # inner repo under mid
        src = r / "pkg"; src.mkdir()
        sup = tdd_verify._suppressed_ancestor(str(src))
        self.assertEqual(sup, (r.resolve(), outer.resolve()))   # outer, NOT mid

    def test_git_less_marker_less_tree_is_silent(self):
        # A2: no `.git` boundary anywhere -> the #01 bound never engaged ->
        # nothing was bounded away -> None.
        proj = self.tmpdir()
        sub = proj / "a" / "b"; sub.mkdir(parents=True)
        self.assertIsNone(tdd_verify._suppressed_ancestor(str(sub)))

    def test_runner_absent_ancestor_marker_is_silent(self):
        # A2 (review F1/F3): a bare ancestor marker that resolves to NO runnable
        # oracle is an ADR-0024 silent-skip, NOT a suppression. A package.json
        # with no test script returns (None,None) from detect_test_command on every
        # host (no runner needed to reject it) -> the probe stays silent.
        ancestor = self.tmpdir()
        (ancestor / "package.json").write_text('{"name":"x","version":"0.0.0"}')
        r = self.inner_repo(ancestor)
        src = r / "pkg"; src.mkdir()
        self.assertIsNone(tdd_verify._suppressed_ancestor(str(src)))

    def test_in_repo_runnable_oracle_is_not_suppression(self):
        # A2 + strict-ancestor guard: a runnable oracle co-located at R's OWN root
        # is not a suppression (it resolves WITHIN bounds; main never probes it).
        # `root in repo_root.parents` excludes self, so even a direct call returns
        # None — the probe can never report a repo as suppressing itself.
        r = self.tmpdir()
        (r / ".git").mkdir()
        self._override(r)                                   # runnable oracle AT R's root
        src = r / "pkg"; src.mkdir()
        self.assertIsNone(tdd_verify._suppressed_ancestor(str(src)))

    def test_no_oracle_above_boundary_is_silent(self):
        # A2: R has a `.git` boundary but there is genuinely no oracle above it
        # -> the honest "no oracle anywhere" skip stays silent.
        ancestor = self.tmpdir()                            # ancestor: no marker
        r = self.inner_repo(ancestor)
        src = r / "pkg"; src.mkdir()
        self.assertIsNone(tdd_verify._suppressed_ancestor(str(src)))

    def test_runnable_oracle_at_home_is_not_suppressed(self):
        # A2 + #01 A7 parity: $HOME is an EXCLUSIVE ceiling in the probe too. Even
        # a RUNNABLE oracle living directly at $HOME, above an inner repo's
        # boundary, is NOT reported as suppressed — $HOME is never a project. Using
        # a runnable override proves the ceiling fires, not mere runner-absence.
        fake_home = self.tmpdir().resolve()
        r = self.inner_repo(fake_home)                      # inner repo under $HOME
        src = r / "pkg"; src.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            self._override(fake_home)                       # runnable oracle AT $HOME
            self.assertIsNone(tdd_verify._suppressed_ancestor(str(src)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
