#!/usr/bin/env python3
"""Tests for install_local.py — local-profile installer helper (#30).

Stdlib unittest only. Run: python3 scripts/test_install_local.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import install_local as il  # noqa: E402

TEMPLATE = HERE.parent / "templates" / "pre-push.sh"


class TestHarnessMerge(unittest.TestCase):
    def _parse(self, text):
        return tomllib.loads(text)

    def test_fresh_file(self):
        out = il.merge_harness_toml("")
        d = self._parse(out)
        self.assertEqual(d["merge-gate"]["profile"], "local")
        local = d["merge-gate"]["local"]
        self.assertEqual(local["enforcement_policy"], "advisory")
        self.assertEqual(local["base_ref"], "auto")
        self.assertEqual(local["artifact_root"], ".codex-review/local")
        self.assertEqual(local["ignore_globs"], [".codex-review/**"])
        self.assertEqual(local["scheduler"]["auto_produce"], "stop-debounced")
        self.assertEqual(local["producer"]["reviewers"], ["codex"])
        self.assertEqual(local["producer"]["codex"]["bin"], "codex")

    def test_lockfiles_stay_in_scope(self):
        d = self._parse(il.merge_harness_toml(""))
        # default review_globs catch everything; ignore_globs only the cache
        self.assertEqual(d["merge-gate"]["local"]["review_globs"], ["**/*"])
        self.assertNotIn("*.lock", d["merge-gate"]["local"]["ignore_globs"])

    def test_preserves_gha_section(self):
        existing = (
            "[merge-gate]\n"
            'profile = "github-actions"\n\n'
            "[merge-gate.github-actions]\n"
            'soft_mode = true\n'
            'bypass_label = "merge-gate-bypass"\n'
        )
        out = il.merge_harness_toml(existing)
        d = self._parse(out)
        self.assertEqual(d["merge-gate"]["profile"], "local")  # flipped
        # GHA section preserved verbatim
        self.assertEqual(d["merge-gate"]["github-actions"]["soft_mode"], True)
        self.assertEqual(d["merge-gate"]["github-actions"]["bypass_label"],
                         "merge-gate-bypass")
        self.assertEqual(d["merge-gate"]["local"]["enforcement_policy"], "advisory")

    def test_preserves_unrelated_tables_and_comments(self):
        existing = "# top comment\n[tool.other]\nkey = 1\n"
        out = il.merge_harness_toml(existing)
        self.assertIn("# top comment", out)
        d = self._parse(out)
        self.assertEqual(d["tool"]["other"]["key"], 1)
        self.assertEqual(d["merge-gate"]["profile"], "local")

    def test_idempotent(self):
        once = il.merge_harness_toml("")
        twice = il.merge_harness_toml(once)
        self.assertEqual(self._parse(once), self._parse(twice))
        # local tables not duplicated
        self.assertEqual(twice.count("[merge-gate.local.producer.codex]"), 1)

    def test_existing_merge_gate_without_profile_key(self):
        existing = "[merge-gate]\n# no profile yet\n"
        d = self._parse(il.merge_harness_toml(existing))
        self.assertEqual(d["merge-gate"]["profile"], "local")


class TestInstallActions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".git" / "hooks").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_pre_push(self):
        dest = il.install_pre_push(self.repo, TEMPLATE)
        self.assertTrue(dest.exists())
        self.assertTrue(os.access(dest, os.X_OK))
        self.assertIn("merge-gate-local verify", dest.read_text())

    def test_ensure_gitignore_adds_once(self):
        self.assertTrue(il.ensure_gitignore(self.repo))
        self.assertFalse(il.ensure_gitignore(self.repo))  # idempotent
        body = (self.repo / ".gitignore").read_text()
        self.assertEqual(body.count(".codex-review/"), 1)

    def test_register_global_hooks_idempotent(self):
        settings = self.repo / "settings.json"
        # seed with an unrelated existing hook to prove we preserve it
        settings.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "python3 other.py"}]}]}}))
        self.assertTrue(il.register_global_hooks(settings))
        self.assertFalse(il.register_global_hooks(settings))  # second is no-op
        data = json.loads(settings.read_text())
        cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn("python3 other.py", cmds)             # preserved
        self.assertIn(il.SCHED_CMD, cmds)                   # added
        post = [h["command"] for g in data["hooks"]["PostToolUse"] for h in g["hooks"]]
        self.assertIn(il.MARK_CMD, post)
        # matcher set on the PostToolUse entry
        matchers = [g.get("matcher") for g in data["hooks"]["PostToolUse"]]
        self.assertIn("Edit|Write", matchers)

    def test_teardown_gha_removes_workflow(self):
        wf = self.repo / ".github" / "workflows" / "codex-review.yml"
        wf.parent.mkdir(parents=True)
        wf.write_text("name: merge-gate\n")
        removed = il.teardown_gha(self.repo)
        self.assertFalse(wf.exists())
        self.assertIn(".github/workflows/codex-review.yml", removed)

    def test_teardown_gha_noop_when_absent(self):
        self.assertEqual(il.teardown_gha(self.repo), [])


class TestC2PreserveLocalSettings(unittest.TestCase):
    """C2 — merge_harness_toml must PRESERVE existing [merge-gate.local*] tables
    on reinstall, never clobber them back to advisory/codex-only defaults. A repo
    that promoted enforcement_policy to "client-side-blocking" or customized
    reviewers must keep those settings; only the profile is forced to local."""

    def _parse(self, text):
        return tomllib.loads(text)

    def test_c2_preserves_promoted_enforcement_and_custom_reviewers(self):
        existing = (
            "[merge-gate]\n"
            'profile = "local"\n\n'
            "[merge-gate.local]\n"
            'enforcement_policy = "client-side-blocking"\n'
            'base_ref = "origin/develop"\n\n'
            "[merge-gate.local.producer]\n"
            'reviewers = ["codex", "claude"]\n'
        )
        out = il.merge_harness_toml(existing)
        d = self._parse(out)
        # Profile still forced local.
        self.assertEqual(d["merge-gate"]["profile"], "local")
        # Pre-fix: these revert to "advisory" / ["codex"]. Post-fix: preserved.
        self.assertEqual(d["merge-gate"]["local"]["enforcement_policy"],
                         "client-side-blocking")
        self.assertEqual(d["merge-gate"]["local"]["base_ref"], "origin/develop")
        self.assertEqual(d["merge-gate"]["local"]["producer"]["reviewers"],
                         ["codex", "claude"])
        # Local tables not duplicated (no re-appended LOCAL_BLOCK).
        self.assertEqual(out.count("[merge-gate.local]"), 1)
        self.assertEqual(out.count("[merge-gate.local.producer]"), 1)


class TestC3PrePushBackup(unittest.TestCase):
    """C3 — install_pre_push must not silently destroy an existing foreign
    pre-push hook (husky / pre-commit / secret-scan / lint). A foreign hook is
    backed up to pre-push.pre-merge-gate; our own hook (carries the marker) is
    overwritten in place; a prior backup is never clobbered."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.hooks = self.repo / ".git" / "hooks"
        self.hooks.mkdir(parents=True)
        self.dest = self.hooks / "pre-push"
        self.backup = self.hooks / "pre-push.pre-merge-gate"

    def tearDown(self):
        self._tmp.cleanup()

    def test_c3_foreign_hook_is_backed_up(self):
        foreign = "#!/bin/sh\necho lint\n"
        self.dest.write_text(foreign)
        il.install_pre_push(self.repo, TEMPLATE)
        # Pre-fix: foreign hook destroyed, no backup. Post-fix: backed up verbatim.
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.backup.read_text(), foreign)
        # dest is now the merge-gate template.
        self.assertIn(il.PRE_PUSH_MARKER, self.dest.read_text())
        self.assertIn("merge-gate-local verify", self.dest.read_text())

    def test_c3_own_hook_overwritten_no_backup(self):
        # A hook that already carries our marker is ours → overwrite in place.
        self.dest.write_text("#!/bin/sh\n# old merge-gate hook\n"
                             f"WRAPPER=$MERGE_GATE_WRAPPER\n# {il.PRE_PUSH_MARKER}\n")
        il.install_pre_push(self.repo, TEMPLATE)
        self.assertFalse(self.backup.exists())  # no backup for our own hook
        self.assertIn("merge-gate-local verify", self.dest.read_text())

    def test_c3_existing_backup_not_clobbered(self):
        # A prior backup wins; a second install must not overwrite it.
        self.backup.write_text("#!/bin/sh\necho ORIGINAL husky\n")
        self.dest.write_text("#!/bin/sh\necho some other foreign hook\n")
        il.install_pre_push(self.repo, TEMPLATE)
        self.assertEqual(self.backup.read_text(),
                         "#!/bin/sh\necho ORIGINAL husky\n")
        # Tie the no-clobber branch to a SUCCESSFUL install, not to inaction: the
        # dest must have received the template even though the backup was kept.
        self.assertIn(il.PRE_PUSH_MARKER, self.dest.read_text())


class TestF1HooksDirResolution(unittest.TestCase):
    """F1 (+ adjacent C3 residual) — install_pre_push must resolve git's REAL
    hooks dir via `git rev-parse --git-path hooks`, not assume <repo>/.git/hooks.
    Pre-fix: a worktree (.git is a FILE) crashed on mkdir, and a repo with
    core.hooksPath (Husky) got a dead hook in a dir git never reads — which ALSO
    defeated the C3 foreign-hook backup (it inspected the wrong file)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.t = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args, cwd=None):
        subprocess.run(["git", "-C", str(cwd or self.t), *args],
                       check=True, capture_output=True, text=True)

    def test_plain_repo_unchanged(self):
        # The common case must still resolve to <repo>/.git/hooks exactly.
        repo = self.t / "repo"
        repo.mkdir()
        self._git("init", "-q", str(repo), cwd=self.t)
        self.assertEqual(il._resolve_hooks_dir(repo), repo / ".git" / "hooks")

    def test_core_hookspath_honored(self):
        # Husky et al. set core.hooksPath; the hook must land THERE, not in
        # .git/hooks where git would never run it.
        repo = self.t / "repo"
        repo.mkdir()
        self._git("init", "-q", str(repo), cwd=self.t)
        self._git("config", "core.hooksPath", ".myhooks", cwd=repo)
        dest = il.install_pre_push(repo, TEMPLATE)
        self.assertEqual(dest, repo / ".myhooks" / "pre-push")
        self.assertTrue(dest.exists())
        self.assertIn("merge-gate-local verify", dest.read_text())
        # NOT written to the assumed-but-wrong .git/hooks location.
        self.assertFalse((repo / ".git" / "hooks" / "pre-push").exists())

    def test_worktree_no_crash_installs_to_common_hooks(self):
        # In a linked worktree .git is a FILE; the old code raised
        # NotADirectoryError on mkdir. Now it must install into git's resolved
        # (common) hooks dir without crashing.
        main = self.t / "main"
        main.mkdir()
        self._git("init", "-q", str(main), cwd=self.t)
        self._git("-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "--allow-empty", "-m", "init", "-q", cwd=main)
        wt = self.t / "wt"
        self._git("worktree", "add", "-q", str(wt), cwd=main)
        self.assertTrue((wt / ".git").is_file())  # precondition: .git is a FILE
        dest = il.install_pre_push(wt, TEMPLATE)   # must NOT raise
        self.assertTrue(dest.exists())
        real = subprocess.run(["git", "-C", str(wt), "rev-parse",
                               "--git-path", "hooks"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(dest.resolve(), (wt / real / "pre-push").resolve())

    def test_c3_backup_works_under_core_hookspath(self):
        # The C3 invariant (never silently skip a foreign hook) must hold under
        # core.hooksPath too. Pre-fix the guard inspected .git/hooks/pre-push
        # (absent on a Husky repo), took no backup, and left the real hook alone.
        repo = self.t / "repo"
        repo.mkdir()
        self._git("init", "-q", str(repo), cwd=self.t)
        self._git("config", "core.hooksPath", ".myhooks", cwd=repo)
        hd = repo / ".myhooks"
        hd.mkdir()
        foreign = "#!/bin/sh\necho lint\n"
        (hd / "pre-push").write_text(foreign)
        il.install_pre_push(repo, TEMPLATE)
        backup = hd / "pre-push.pre-merge-gate"
        self.assertTrue(backup.exists())               # foreign hook preserved
        self.assertEqual(backup.read_text(), foreign)
        self.assertIn(il.PRE_PUSH_MARKER, (hd / "pre-push").read_text())


class TestCLI(unittest.TestCase):
    def test_full_local_install(self):
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            settings = Path(t) / "settings.json"
            r = subprocess.run(
                [sys.executable, str(HERE / "install_local.py"),
                 "--repo", str(repo), "--settings", str(settings),
                 "--pre-push-template", str(TEMPLATE)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((repo / "harness.toml").exists())
            self.assertTrue((repo / ".git" / "hooks" / "pre-push").exists())
            self.assertTrue((repo / ".gitignore").exists())
            self.assertTrue(settings.exists())
            d = tomllib.loads((repo / "harness.toml").read_text())
            self.assertEqual(d["merge-gate"]["profile"], "local")


if __name__ == "__main__":
    unittest.main()
