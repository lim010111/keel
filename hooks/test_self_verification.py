#!/usr/bin/env python3
"""Tests for the self-verification gate (issue self-verify#01 E2).

Exercises the dormant commit-msg hook + PreToolUse recorder by driving them the
way git would (the hook gets the staged index + a message file), with the
out-of-repo audit root redirected via SELF_VERIFICATION_STATE_ROOT so tests never
touch the real ~/.claude/.scratch.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import self_verification as sv  # noqa: E402
import self_verification_pretooluse as pt  # noqa: E402

HOOK = HERE / "self_verification.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

ORACLE = "sh -c '[ -f PASS ]'"  # green iff a PASS file is in the tree under test

HARNESS_TOML = (
    "[self-verification]\n"
    f'test = "{ORACLE}"\n'
    'bypass_trailer = "Self-Verify-Bypass"\n'
    'exempt_globs = ["docs/**", "**/*.md"]\n'
)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sv-test-")
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        self.state = Path(self.tmp) / "state"
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "harness.toml").write_text(HARNESS_TOML)
        (self.repo / "seed.txt").write_text("seed\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "seed")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp])

    def run_hook(self, msg="a commit message\n"):
        msg_file = Path(self.tmp) / "MSG"
        msg_file.write_text(msg)
        env = {**os.environ, **GIT_ENV,
               "SELF_VERIFICATION_STATE_ROOT": str(self.state),
               "HOME": self.tmp}
        r = subprocess.run([sys.executable, str(HOOK), str(msg_file)],
                           cwd=str(self.repo), capture_output=True, text=True, env=env)
        return r

    @contextlib.contextmanager
    def _stub_bin(self, name, exit_code):
        # put a fake program on PATH so 'real invocation' tests do not depend on
        # the tool being installed; the oracle runs via shell + inherits PATH.
        bindir = Path(self.tmp) / "stubbin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / name
        stub.write_text(f"#!/bin/sh\nexit {exit_code}\n")
        stub.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}{os.pathsep}{old}"
        try:
            yield bindir
        finally:
            os.environ["PATH"] = old

    def _pytest_stub(self, exit_code):
        return self._stub_bin("pytest", exit_code)

    def sdir(self):
        return Path(self.state) / sv.repo_hash(self.repo)

    def read_jsonl(self, name):
        p = self.sdir() / name
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


class OracleBlockTests(Base):
    def test_green_oracle_allows(self):
        (self.repo / "PASS").write_text("")
        git(self.repo, "add", "PASS")
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_red_oracle_blocks(self):
        # change a tracked file but never make PASS -> oracle red -> block (#1-3).
        (self.repo / "seed.txt").write_text("changed\n")
        git(self.repo, "add", "seed.txt")
        r = self.run_hook()
        self.assertEqual(r.returncode, 1)
        self.assertIn("RED", r.stderr)

    def test_A5_runs_against_staged_not_working_tree(self):
        # PASS exists in the working tree but is NOT staged -> the committed tree
        # has no PASS -> the hook must see RED (proves staged-tree isolation).
        (self.repo / "seed.txt").write_text("changed\n")
        git(self.repo, "add", "seed.txt")
        (self.repo / "PASS").write_text("")  # present in working tree, unstaged
        r = self.run_hook()
        self.assertEqual(r.returncode, 1)

    def test_A5_staged_pass_attested_even_if_deleted_in_working_tree(self):
        (self.repo / "PASS").write_text("")
        git(self.repo, "add", "PASS")            # PASS staged
        os.remove(self.repo / "PASS")            # removed from working tree only
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_timeout_is_fail_not_infra(self):
        # a hung oracle must count as RED (fail-closed), never 'infra' (fail-open)
        (self.repo / "PASS").write_text("")
        git(self.repo, "add", "PASS")
        tree = sv.staged_tree_sha(self.repo)
        old = sv.ORACLE_TIMEOUT_SECONDS
        sv.ORACLE_TIMEOUT_SECONDS = 1
        try:
            verdict, _ = sv.run_oracles(self.repo, tree, [("test", "sleep 5")])
        finally:
            sv.ORACLE_TIMEOUT_SECONDS = old
        self.assertEqual(verdict, "fail")

    def test_nonutf8_failing_oracle_is_fail_not_infra(self):
        # a failing oracle emitting non-UTF-8 bytes must not decode-crash into
        # 'infra' (fail-open) — errors='replace' keeps it RED
        (self.repo / "PASS").write_text("")
        git(self.repo, "add", "PASS")
        tree = sv.staged_tree_sha(self.repo)
        verdict, _ = sv.run_oracles(
            self.repo, tree, [("test", r"printf '\377\376'; exit 1")])
        self.assertEqual(verdict, "fail")

    def test_F0_config_read_from_staged_not_working_tree(self):
        # the STAGED harness.toml keeps the PASS oracle (green, PASS is staged);
        # the dirty WORKING-TREE harness.toml points at an always-red oracle but
        # is NOT staged. The attestation must use the config in the tree being
        # committed -> green -> allow (F0).
        (self.repo / "PASS").write_text("")
        git(self.repo, "add", "PASS")
        (self.repo / "harness.toml").write_text(
            '[self-verification]\ntest = "false"\n')  # dirty, unstaged, always RED
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_F1_run_oracles_empty_only_for_real_pytest_exit5(self):
        # a REAL pytest invocation exiting 5 ("no tests collected") -> 'empty'
        # (recorded, not green). A non-pytest exit 5 — or a command that merely
        # contains the substring 'pytest' (finding H) — stays a plain failure.
        (self.repo / "PASS").write_text("")
        git(self.repo, "add", "PASS")
        tree = sv.staged_tree_sha(self.repo)
        with self._pytest_stub(exit_code=5):
            verdict, _ = sv.run_oracles(self.repo, tree, [("test", "pytest -q")])
        self.assertEqual(verdict, "empty")
        verdict2, _ = sv.run_oracles(self.repo, tree, [("test", "exit 5")])
        self.assertEqual(verdict2, "fail")
        # substring-only 'pytest' must NOT be treated as an empty pytest (H)
        verdict3, _ = sv.run_oracles(self.repo, tree, [("test", "sh -c 'exit 5'  # pytest")])
        self.assertEqual(verdict3, "fail")

    def test_F1_run_oracles_empty_multi_oracle(self):
        # empties-but-no-failures across multiple oracles -> 'empty'; any failure
        # dominates (fail > empty > pass).
        (self.repo / "PASS").write_text("")
        git(self.repo, "add", "PASS")
        tree = sv.staged_tree_sha(self.repo)
        with self._pytest_stub(exit_code=5):
            v_empty, _ = sv.run_oracles(
                self.repo, tree, [("test", "pytest -q"), ("lint", "true")])
            v_fail, _ = sv.run_oracles(
                self.repo, tree, [("test", "pytest -q"), ("lint", "false")])
        self.assertEqual(v_empty, "empty")
        self.assertEqual(v_fail, "fail")

    def test_F1_wrapper_pytest_exit5_is_empty_not_block(self):
        # re-review regression: `uv run pytest` exit 5 must route to 'empty'
        # (record-not-block), not 'fail'/BLOCK as the over-narrow H check did.
        (self.repo / "PASS").write_text("")
        git(self.repo, "add", "PASS")
        tree = sv.staged_tree_sha(self.repo)
        with self._stub_bin("uv", exit_code=5):
            verdict, _ = sv.run_oracles(self.repo, tree, [("test", "uv run pytest")])
        self.assertEqual(verdict, "empty")

    def test_F1_hook_records_empty_oracle_and_allows(self):
        # end-to-end: a real pytest oracle that collects nothing must be RECORDED
        # to the audit lane, never silently passed (F1 fail-open hole). Exercises
        # the staged-config read (F0) too: the committed harness.toml is the oracle.
        (self.repo / "harness.toml").write_text(
            '[self-verification]\ntest = "pytest -q"\n')
        git(self.repo, "add", "harness.toml")
        git(self.repo, "commit", "-q", "-m", "pytest oracle")
        (self.repo / "f.txt").write_text("x\n")
        git(self.repo, "add", "f.txt")
        with self._pytest_stub(exit_code=5):
            r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        recs = self.read_jsonl("bypass.jsonl")
        self.assertTrue(any(x["event"] == "empty-oracle" for x in recs))


class BypassTests(Base):
    def test_B5_trailer_records_and_allows(self):
        (self.repo / "seed.txt").write_text("changed\n")
        git(self.repo, "add", "seed.txt")
        r = self.run_hook("deliberate red\n\nSelf-Verify-Bypass: commit-the-red-test\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        recs = self.read_jsonl("bypass.jsonl")
        self.assertTrue(any(x["event"] == "bypass-trailer"
                            and x["reason"] == "commit-the-red-test" for x in recs))

    def test_C1_records_are_out_of_repo(self):
        (self.repo / "seed.txt").write_text("changed\n")
        git(self.repo, "add", "seed.txt")
        self.run_hook("x\n\nSelf-Verify-Bypass: r\n")
        self.assertTrue((self.sdir() / "bypass.jsonl").exists())
        # nothing written inside the repo
        self.assertFalse((self.repo / ".scratch" / "verification-gate").exists())


class NoOracleTests(Base):
    def _strip_section(self):
        (self.repo / "harness.toml").write_text("[merge-gate]\nprofile='local'\n")
        git(self.repo, "add", "harness.toml")
        git(self.repo, "commit", "-q", "-m", "drop sv section")

    def test_A6_no_oracle_records_and_allows(self):
        self._strip_section()
        (self.repo / "x.py").write_text("y = 1\n")
        git(self.repo, "add", "x.py")
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        recs = self.read_jsonl("bypass.jsonl")
        self.assertTrue(any(x["event"] == "no-oracle" for x in recs))

    def test_A_autodetect_not_consulted_on_attestation_path(self):
        # F0/A5 completion: oracle SELECTION must come from the tree under
        # attestation on EVERY path, not just the explicitly-declared one. The
        # STAGED tree declares no [self-verification] oracle. A dirty, UNSTAGED
        # .claude/tdd-test-cmd would, under the old working-tree auto-detect
        # fallback, be read verbatim AS the oracle and govern a commit it is not
        # part of (the auto-detect analog of the F0 working-tree leak; here it
        # selects an always-RED oracle that would wrongly BLOCK). The commit-msg
        # attestation must NOT consult the working tree: no explicit staged
        # oracle => A6 no-oracle = record-and-allow. (A2's auto-detect stays
        # correct for tdd_verify's Stop-time working-tree feedback.)
        self._strip_section()
        (self.repo / "x.py").write_text("y = 1\n")
        git(self.repo, "add", "x.py")
        cdir = self.repo / ".claude"
        cdir.mkdir(exist_ok=True)
        (cdir / "tdd-test-cmd").write_text("false\n")  # unstaged; always-RED if detected
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)  # must not block on a working-tree-only oracle
        recs = self.read_jsonl("bypass.jsonl")
        self.assertTrue(any(x["event"] == "no-oracle" for x in recs))


class WeakeningObserveTests(Base):
    def _commit_test_file(self):
        (self.repo / "test_x.py").write_text(
            "def test_a():\n    assert 1 + 1 == 2\n"
        )
        git(self.repo, "add", "test_x.py")
        git(self.repo, "commit", "-q", "-m", "add test")

    def test_B3B4_added_skip_recorded_but_allowed(self):
        self._commit_test_file()
        (self.repo / "PASS").write_text("")
        (self.repo / "test_x.py").write_text(
            "import pytest\n\n@pytest.mark.skip\ndef test_a():\n    assert 1 + 1 == 2\n"
        )
        git(self.repo, "add", "PASS", "test_x.py")
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)  # observe = allow
        recs = self.read_jsonl("weakening.jsonl")
        self.assertTrue(any(x["kind"] == "#5" and "added skip" in x["detail"]
                            for x in recs))

    def test_removed_test_recorded(self):
        self._commit_test_file()
        (self.repo / "PASS").write_text("")
        (self.repo / "test_x.py").write_text("# emptied\n")
        git(self.repo, "add", "PASS", "test_x.py")
        self.run_hook()
        recs = self.read_jsonl("weakening.jsonl")
        self.assertTrue(any(x["kind"] == "#5" for x in recs))

    def test_config_fail_under_recorded(self):
        (self.repo / "PASS").write_text("")
        (self.repo / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 50\n")
        git(self.repo, "add", "PASS", "pyproject.toml")
        self.run_hook()
        recs = self.read_jsonl("weakening.jsonl")
        self.assertTrue(any(x["kind"] == "#6" for x in recs))

    def test_B_harness_oracle_change_recorded(self):
        # Task B: weakening the oracle POLICY itself — changing the staged
        # harness.toml [self-verification] test command (here to the always-green
        # `true`) — must be RECORDED to the weakening ledger (observe-mode) and
        # still ALLOW the commit. The #4-6 line-scan ignores harness.toml, so
        # without this a neutered oracle self-attests green with zero trace. (HEAD
        # has test = the PASS-checking ORACLE from Base.setUp.)
        (self.repo / "PASS").write_text("")
        (self.repo / "harness.toml").write_text(
            '[self-verification]\ntest = "true"\n'
            'bypass_trailer = "Self-Verify-Bypass"\n')
        git(self.repo, "add", "PASS", "harness.toml")
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)  # observe = allow
        recs = self.read_jsonl("weakening.jsonl")
        self.assertTrue(any(x["kind"] == "#6"
                            and x["locator"] == "harness.toml:self-verification.test"
                            for x in recs))

    def test_B_harness_oracle_removal_recorded(self):
        # the most blatant neuter — removing the test oracle entirely (deleting
        # the key / emptying the section) — must record a '#6' removal row, not
        # escape as a non-change. (HEAD has the test oracle from Base.setUp.)
        (self.repo / "PASS").write_text("")
        (self.repo / "harness.toml").write_text(
            '[self-verification]\nbypass_trailer = "Self-Verify-Bypass"\n')  # test removed
        git(self.repo, "add", "PASS", "harness.toml")
        self.run_hook()
        recs = self.read_jsonl("weakening.jsonl")
        self.assertTrue(any(x["kind"] == "#6"
                            and x["locator"] == "harness.toml:self-verification.test"
                            and "removed" in x["detail"] for x in recs))

    def test_B_added_oracle_is_strengthening_not_recorded(self):
        # adding a NEW oracle kind (absent at HEAD) is a strengthening; the test
        # oracle is unchanged. Neither must record a weakening row.
        (self.repo / "PASS").write_text("")
        (self.repo / "harness.toml").write_text(HARNESS_TOML + 'lint = "true"\n')
        git(self.repo, "add", "PASS", "harness.toml")
        self.run_hook()
        self.assertEqual(self.read_jsonl("weakening.jsonl"), [])

    def test_B_non_oracle_harness_edit_not_recorded(self):
        # editing a NON-oracle harness.toml key (bypass_trailer) with the oracle
        # commands unchanged must not record a weakening (no FP on innocent edits).
        (self.repo / "PASS").write_text("")
        (self.repo / "harness.toml").write_text(
            "[self-verification]\n"
            f'test = "{ORACLE}"\n'                # oracle unchanged
            'bypass_trailer = "Changed-Trailer"\n')  # only the non-oracle key changed
        git(self.repo, "add", "PASS", "harness.toml")
        self.run_hook()
        self.assertEqual(self.read_jsonl("weakening.jsonl"), [])

    def test_B_malformed_staged_config_does_not_flood(self):
        # a malformed STAGED harness.toml must NOT flood the ledger with spurious
        # 'oracle removed' rows from a mere syntax typo (it would poison the
        # observe->enforce calibration dataset). The malformed config already
        # surfaces via the Task A no-oracle record; the policy detector skips it.
        (self.repo / "PASS").write_text("")
        (self.repo / "harness.toml").write_text(
            '[self-verification\ntest = "true"\n')  # broken: unclosed section header
        git(self.repo, "add", "PASS", "harness.toml")
        self.run_hook()
        recs = self.read_jsonl("weakening.jsonl")
        self.assertEqual([r for r in recs if r["kind"] == "#6"], [])

    def test_B_malformed_head_baseline_records_nothing(self):
        # symmetric to the malformed-STAGED guard: a malformed HEAD harness.toml
        # has no parseable prior policy to weaken, so a staged oracle change
        # records no weakening row (and never crashes). FN tolerated on the
        # observe lane — the unparseable prior config was already surfaced as
        # no-oracle at its OWN commit; we do not synthesise a low-signal row.
        (self.repo / "harness.toml").write_text(
            '[self-verification\ntest = "pytest"\n')  # broken HEAD baseline
        git(self.repo, "add", "harness.toml")
        git(self.repo, "commit", "-q", "-m", "malformed config")
        (self.repo / "PASS").write_text("")
        (self.repo / "harness.toml").write_text(
            '[self-verification]\ntest = "true"\n'
            'bypass_trailer = "Self-Verify-Bypass"\n')  # valid staged; oracle weakened
        git(self.repo, "add", "PASS", "harness.toml")
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        recs = self.read_jsonl("weakening.jsonl")
        self.assertEqual([x for x in recs if x["kind"] == "#6"], [])

    def test_exempt_path_not_flagged(self):
        # a .md file adding a skip-looking line must not be flagged (exempt glob)
        (self.repo / "PASS").write_text("")
        (self.repo / "notes.md").write_text("@pytest.mark.skip\n")
        git(self.repo, "add", "PASS", "notes.md")
        self.run_hook()
        self.assertEqual(self.read_jsonl("weakening.jsonl"), [])


class UnitTests(unittest.TestCase):
    def test_resolve_oracles_prefers_config(self):
        cfg = {"oracles": [("test", "X")], "section_present": True,
               "exempt_globs": [], "bypass_trailer": "B"}
        oracles, reason = sv.resolve_oracles(Path("/nonexistent"), cfg)
        self.assertEqual(oracles, [("test", "X")])
        self.assertIsNone(reason)

    def test_resolve_oracles_empty_section_reason(self):
        cfg = {"oracles": [], "section_present": True,
               "exempt_globs": [], "bypass_trailer": "B"}
        with tempfile.TemporaryDirectory() as d:
            oracles, reason = sv.resolve_oracles(Path(d), cfg)
        self.assertEqual(oracles, [])
        self.assertEqual(reason, "config-empty")

    def test_parse_bypass_trailer(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("subj\n\nSelf-Verify-Bypass: because reasons\n")
            name = f.name
        self.assertEqual(sv.parse_bypass_trailer(name, "Self-Verify-Bypass"),
                         "because reasons")
        self.assertIsNone(sv.parse_bypass_trailer(name, "Other-Trailer"))
        os.remove(name)

    def test_append_jsonl_swallows_write_failure(self):
        # an unwritable path must return False, never raise (best-effort-then-allow)
        bad = Path("/proc/nonexistent-dir-xyz/sub/bypass.jsonl")
        self.assertFalse(sv.append_jsonl(bad, {"a": 1}))

    def test_run_oracle_cmd_timeout_and_decode(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sv._run_oracle_cmd("sleep 5", d, 1)[2], "timeout")
            rc, out, status = sv._run_oracle_cmd("exit 3", d, 10)
            self.assertEqual((rc, status), (3, "ran"))
            # non-UTF-8 output must not raise (errors='replace')
            rc, out, status = sv._run_oracle_cmd(r"printf '\377'; exit 1", d, 10)
            self.assertEqual((rc, status), (1, "ran"))

    def test_F2_oracle_env_strips_git_vars(self):
        # the oracle subprocess must NOT inherit git's hook-injected GIT_* vars
        # (else an oracle that shells out to git would touch the parent index).
        os.environ["GIT_DIR"] = "/tmp/fake-parent/.git"
        os.environ["GIT_INDEX_FILE"] = "/tmp/fake-parent/.git/index"
        try:
            with tempfile.TemporaryDirectory() as d:
                rc, out, status = sv._run_oracle_cmd(
                    'printf "DIR=[%s] IDX=[%s]" "$GIT_DIR" "$GIT_INDEX_FILE"',
                    d, 10)
        finally:
            os.environ.pop("GIT_DIR", None)
            os.environ.pop("GIT_INDEX_FILE", None)
        self.assertEqual(status, "ran")
        self.assertIn("DIR=[]", out)
        self.assertIn("IDX=[]", out)

    def test_exempt_recursive_glob(self):
        self.assertTrue(sv._exempt("notes.md", ["**/*.md"]))      # top-level
        self.assertTrue(sv._exempt("a/b/c.md", ["**/*.md"]))      # nested
        self.assertTrue(sv._exempt("docs/x/y.py", ["docs/**"]))
        self.assertFalse(sv._exempt("src/x.py", ["**/*.md"]))

    def test_pretooluse_classify(self):
        self.assertEqual(pt.classify("git commit --no-verify -m x")[0],
                         "no-verify-commit")
        self.assertEqual(pt.classify("git commit -nm x")[0], "no-verify-commit")
        self.assertEqual(pt.classify("python -m pytest -k foo")[0],
                         "scope-narrowed-test")
        self.assertEqual(pt.classify("pytest tests/test_x.py::test_a")[0],
                         "scope-narrowed-test")
        self.assertIsNone(pt.classify("python -m pytest -q")[0])
        self.assertIsNone(pt.classify("ls -la")[0])

    def test_pretooluse_no_false_positive_on_chained_commands(self):
        # the bypass flag must be read from the commit's own argv, not a chained
        # command after && / ; (else the observe->enforce dataset is poisoned)
        self.assertIsNone(pt.classify("git commit -m x && grep -rn foo")[0])
        self.assertIsNone(pt.classify("git commit -m x; git status -uno")[0])
        self.assertIsNone(pt.classify('git commit -m "fix-main"')[0])
        self.assertIsNone(pt.classify("git commit -m x && git add -n .")[0])

    def test_F4_no_false_positive_on_flag_in_message_text(self):
        # `-n` / `pytest -k` sitting inside a quoted message or another command's
        # argument must NOT be misread as a bypass (the F4 dataset-poisoning FP).
        self.assertIsNone(pt.classify('git commit -m "add -n flag"')[0])
        self.assertIsNone(pt.classify('git commit -m "run pytest -k foo locally"')[0])
        self.assertIsNone(pt.classify('echo "pytest -k foo"')[0])
        self.assertIsNone(pt.classify('git commit -m "see test_x.py for repro"')[0])
        # `-mn` is `-m` consuming `n` as its message, NOT --no-verify
        self.assertIsNone(pt.classify("git commit -mn")[0])
        # real bypasses still detected even when the message text also holds `-n`
        self.assertEqual(pt.classify('git commit -nm "msg"')[0], "no-verify-commit")
        self.assertEqual(pt.classify('git commit --no-verify -m "add -n flag"')[0],
                         "no-verify-commit")

    def test_F4_signing_flag_does_not_swallow_no_verify(self):
        # finding A: -S/--gpg-sign take an attached-only optional arg, so a
        # SPACE-separated `-n` after them is real --no-verify and must be recorded.
        self.assertEqual(pt.classify("git commit -S -n -m x")[0], "no-verify-commit")
        self.assertEqual(pt.classify("git commit --gpg-sign -n -m x")[0],
                         "no-verify-commit")
        # attached signing forms with no -n are NOT bypasses
        self.assertIsNone(pt.classify("git commit -Sn -m x")[0])   # -S keyid 'n'
        self.assertIsNone(pt.classify("git commit -Skey -m x")[0])
        self.assertIsNone(pt.classify("git commit --gpg-sign=key -m x")[0])

    def test_F4_commit_must_be_real_subcommand(self):
        # finding C: `commit` as a --grep/--author term or a ref is not the subcommand
        self.assertIsNone(pt.classify("git log --grep commit -n 20")[0])
        self.assertIsNone(pt.classify("git log --author commit -n 5")[0])
        self.assertIsNone(pt.classify("git log commit -n 5")[0])
        # finding D: git as another program's argument is not a commit invocation
        self.assertIsNone(pt.classify("echo git commit -n >> notes.txt")[0])
        # real commits still detected: global opts, env-assignment prefix, abs path
        self.assertEqual(pt.classify("git -C /repo commit -n")[0], "no-verify-commit")
        self.assertEqual(pt.classify("git -c user.name=x commit -n")[0],
                         "no-verify-commit")
        self.assertEqual(pt.classify("FOO=bar git commit -n")[0], "no-verify-commit")
        self.assertEqual(pt.classify("/usr/bin/git commit -n")[0], "no-verify-commit")

    def test_F4_end_of_options_separator(self):
        # finding E: a pathspec named like a flag after `--` is not an option
        self.assertIsNone(pt.classify("git commit -m x -- -n.txt")[0])
        # a real --no-verify before `--` is still caught
        self.assertEqual(pt.classify("git commit --no-verify -- -n.txt")[0],
                         "no-verify-commit")

    def test_F4_pytest_program_name_strict(self):
        # finding G: only genuine CPython launchers, not pythonista-like names
        self.assertEqual(pt.classify("python3.11 -m pytest -k foo")[0],
                         "scope-narrowed-test")
        self.assertEqual(pt.classify("/usr/bin/python3 -m pytest -k foo")[0],
                         "scope-narrowed-test")
        self.assertEqual(pt.classify("py.test -k foo")[0], "scope-narrowed-test")
        self.assertIsNone(pt.classify("pythonista -m pytest -k foo")[0])

    def test_F4_valueerror_fallback_recovers_short_n(self):
        # finding F: on a shlex parse failure (unbalanced quote) the fallback must
        # still catch the short `-n`, not only the long --no-verify.
        self.assertEqual(pt.classify('git commit -n -m "unterminated')[0],
                         "no-verify-commit")
        self.assertEqual(pt.classify('git commit --no-verify -m "unterminated')[0],
                         "no-verify-commit")

    def test_cmd_invokes_pytest_wrappers_and_glued(self):
        # re-review: shared pytest detection recognises wrappers + glued -m, not
        # just direct pytest, and still rejects substrings / indirect wrappers.
        for ok in ("pytest -q", "py.test", "python -m pytest", "python3.11 -m pytest -q",
                   "python -mpytest", "uv run pytest", "poetry run pytest -q",
                   "pdm run pytest", "uv run python -m pytest", "/usr/bin/pytest"):
            self.assertTrue(sv._cmd_invokes_pytest(ok), ok)
        for no in ("make pytest-ci", "tox -e py", "python -m not_pytest",
                   "echo pytest", "sh -c 'exit 5'  # pytest", "exit 5",
                   "uv run", "uv run foo pytest"):
            self.assertFalse(sv._cmd_invokes_pytest(no), no)
        # pathological wrapper nesting must not RecursionError (iterative peel)
        self.assertTrue(sv._cmd_invokes_pytest("uv run " * 2000 + "pytest"))
        self.assertFalse(sv._cmd_invokes_pytest("uv run " * 2000 + "make x"))

    def test_F4_newline_separated_commands(self):
        # re-review finding (high): newline is a command separator. A real bypass
        # on a later line must be detected; an unrelated later line must not poison
        # the commit; a newline INSIDE a quoted message stays one commit segment.
        self.assertEqual(
            pt.classify("git add -A\ngit commit --no-verify -m x")[0],
            "no-verify-commit")
        self.assertEqual(
            pt.classify("source venv/bin/activate\npytest -k foo")[0],
            "scope-narrowed-test")
        self.assertIsNone(pt.classify('git commit -m "wip"\ngrep -rn foo')[0])
        self.assertIsNone(pt.classify("pytest\ngrep -k foo bar")[0])
        self.assertIsNone(pt.classify('git commit -m "line1\nline2"')[0])

    def test_F4_grouped_commands(self):
        # re-review finding (low): subshell/brace grouping must still anchor the
        # real program, not the leading ( or {.
        self.assertEqual(pt.classify("(git commit -n)")[0], "no-verify-commit")
        self.assertEqual(pt.classify("( git commit -n )")[0], "no-verify-commit")
        self.assertEqual(pt.classify("{ git commit -n; }")[0], "no-verify-commit")
        self.assertEqual(pt.classify("(pytest -k foo)")[0], "scope-narrowed-test")

    def test_F4_wrapper_pytest_scope_narrowed(self):
        # re-review: scope-narrowed detection mirrors the wrapper-aware pytest check
        self.assertEqual(pt.classify("uv run pytest -k foo")[0], "scope-narrowed-test")
        self.assertEqual(
            pt.classify("poetry run pytest tests/test_x.py::test_a")[0],
            "scope-narrowed-test")
        self.assertIsNone(pt.classify("uv run pytest")[0])  # not narrowed


if __name__ == "__main__":
    unittest.main()
