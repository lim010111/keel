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
        self.assertEqual(local["artifact_root"], ".merge-gate/local")
        self.assertEqual(local["ignore_globs"], [".merge-gate/**"])
        self.assertEqual(local["scheduler"]["auto_produce"], "stop-debounced")
        self.assertEqual(local["producer"]["reviewers"], ["codex"])
        self.assertEqual(local["producer"]["codex"]["bin"], "codex")

    def test_fresh_file_ships_commented_model_keys(self):
        # #47/#48 — the model/effort knobs ship COMMENTED (unset = each tool's
        # own default) so harness.toml self-documents; the validator table
        # itself is comment-only, so it must not parse as a real table on a
        # fresh install (load_config sees no validator keys → tool defaults).
        out = il.merge_harness_toml("")
        self.assertIn('# model            = "gpt-5.5"', out)
        self.assertIn('# reasoning_effort = "high"', out)
        self.assertIn("[merge-gate.local.validator]", out)
        self.assertIn('# model             = "sonnet"', out)
        self.assertIn('# dispatcher_model  = "opus"', out)
        self.assertIn('# dispatcher_effort = "medium"', out)
        # official-name guides present (#48)
        self.assertIn("gpt-5.4-mini", out)
        self.assertIn("claude-opus-4-8", out)
        d = self._parse(out)
        self.assertEqual(d["merge-gate"]["local"].get("validator", {}), {})
        self.assertNotIn("model", d["merge-gate"]["local"]["producer"]["codex"])
        self.assertNotIn("claude", d["merge-gate"]["local"]["producer"])

    def test_reinstall_preserves_customized_validator_table(self):
        # C2 extension (#47): a repo that set real validator models must not be
        # reverted to the commented defaults on reinstall.
        once = il.merge_harness_toml("")
        custom = once.replace(
            '# model             = "sonnet"          # validator AGENT (judgment subagent) — tier alias only: haiku|sonnet|opus|fable; unset = agent default',
            'model             = "opus"')
        self.assertNotEqual(once, custom)  # the replace actually hit
        again = il.merge_harness_toml(custom)
        d = self._parse(again)
        self.assertEqual(d["merge-gate"]["local"]["validator"]["model"], "opus")
        self.assertEqual(again.count("[merge-gate.local.validator]"), 1)

    def test_lockfiles_stay_in_scope(self):
        d = self._parse(il.merge_harness_toml(""))
        # default review_globs catch everything; ignore_globs only the cache
        self.assertEqual(d["merge-gate"]["local"]["review_globs"], ["**/*"])
        self.assertNotIn("*.lock", d["merge-gate"]["local"]["ignore_globs"])

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

    def test_equivalent_header_spelling_edited_not_duplicated(self):
        # `[ merge-gate ]` declares the SAME table as `[merge-gate]` (TOML folds
        # whitespace around a bare key). The old exact-string compare missed it,
        # appended a second [merge-gate] and produced a duplicate-table parse
        # failure — the record_profile codex:finding-0 class, fixed here via the
        # shared toml_sections.section_is.
        existing = "[ merge-gate ]\n# annotated\nsoft = true\n"
        out = il.merge_harness_toml(existing)
        d = self._parse(out)                      # raises if a table is declared twice
        self.assertEqual(d["merge-gate"]["profile"], "local")
        self.assertEqual(d["merge-gate"]["soft"], True)
        self.assertIn("# annotated", out)

    def test_equivalent_local_table_spelling_not_duplicated(self):
        # An existing quoted/spaced local table must count as "has local" so the
        # canonical LOCAL_BLOCK is not appended a second time.
        once = il.merge_harness_toml("")
        spaced = once.replace("[merge-gate.local]", '[ merge-gate . "local" ]', 1)
        twice = il.merge_harness_toml(spaced)
        self._parse(twice)                        # still valid TOML, no dup tables
        self.assertEqual(twice.count("enforcement_policy"), 1)


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
        self.assertEqual(body.count(".merge-gate/"), 1)

    def test_deregister_stale_hooks_removes_retired_pair(self):
        # #33 — the Stop scheduler + PostToolUse mark are RETIRED (ADR-0014). The
        # installer no longer registers them and cleans any stale registrations
        # left on the machine, preserving every unrelated hook.
        settings = self.repo / "settings.json"
        settings.write_text(json.dumps({"hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "python3 other.py"}]},
                {"hooks": [{"type": "command", "command": il.SCHED_CMD}]},
            ],
            "PostToolUse": [
                {"matcher": "Edit|Write",
                 "hooks": [{"type": "command", "command": il.MARK_CMD}]},
            ],
        }}))
        self.assertTrue(il.deregister_stale_hooks(settings))   # removed the retired pair
        self.assertFalse(il.deregister_stale_hooks(settings))  # idempotent (no-op now)
        data = json.loads(settings.read_text())
        stop_cmds = [h["command"] for g in data["hooks"].get("Stop", []) for h in g["hooks"]]
        self.assertIn("python3 other.py", stop_cmds)           # unrelated preserved
        self.assertNotIn(il.SCHED_CMD, stop_cmds)              # stale scheduler gone
        post_cmds = [h["command"] for g in data["hooks"].get("PostToolUse", [])
                     for h in g["hooks"]]
        self.assertNotIn(il.MARK_CMD, post_cmds)               # stale mark gone

    def test_install_does_not_register_retired_hooks(self):
        # A fresh install must NOT add the Stop/PostToolUse hooks any more.
        settings = self.repo / "settings.json"
        settings.write_text(json.dumps({"hooks": {}}))
        il.deregister_stale_hooks(settings)
        data = json.loads(settings.read_text())
        cmds = [h.get("command")
                for grp in data.get("hooks", {}).values()
                for g in grp for h in g.get("hooks", [])]
        self.assertNotIn(il.SCHED_CMD, cmds)
        self.assertNotIn(il.MARK_CMD, cmds)


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
            self.assertTrue((repo / ".git" / "hooks" / "post-commit").exists())
            self.assertTrue((repo / ".gitignore").exists())
            # #33 — the installer no longer registers any GLOBAL hooks (the Stop
            # scheduler + PostToolUse mark are retired), so on a clean machine it
            # leaves settings.json untouched (nothing stale to remove).
            d = tomllib.loads((repo / "harness.toml").read_text())
            self.assertEqual(d["merge-gate"]["profile"], "local")

    def test_full_local_install_writes_post_commit_and_no_stale_hooks(self):
        # #33 — a full install lands the post-commit trigger and does NOT register
        # the retired Stop/PostToolUse hooks.
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            settings = Path(t) / "settings.json"
            r = subprocess.run(
                [sys.executable, str(HERE / "install_local.py"),
                 "--repo", str(repo), "--settings", str(settings)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            pc = repo / ".git" / "hooks" / "post-commit"
            self.assertTrue(pc.exists())
            self.assertTrue(os.access(pc, os.X_OK))
            self.assertIn(il.POST_COMMIT_MARKER, pc.read_text())
            # On a clean machine no settings.json is written (nothing to register
            # or deregister); if it exists, it must not carry the retired hooks.
            data = json.loads(settings.read_text()) if settings.exists() else {}
            cmds = [h.get("command")
                    for grp in data.get("hooks", {}).values()
                    for g in grp for h in g.get("hooks", [])]
            self.assertNotIn(il.SCHED_CMD, cmds)
            self.assertNotIn(il.MARK_CMD, cmds)


POST_TEMPLATE = HERE.parent / "templates" / "post-commit"


class TestPostCommitInstall(unittest.TestCase):
    """#33 4e — install the post-commit hook mirroring install_pre_push's marker
    + foreign-backup convention (post-commit.pre-merge-gate)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.hooks = self.repo / ".git" / "hooks"
        self.hooks.mkdir(parents=True)
        self.dest = self.hooks / "post-commit"
        self.backup = self.hooks / "post-commit.pre-merge-gate"

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_basic(self):
        dest = il.install_post_commit(self.repo, POST_TEMPLATE)
        self.assertTrue(dest.exists())
        self.assertTrue(os.access(dest, os.X_OK))
        self.assertIn(il.POST_COMMIT_MARKER, dest.read_text())

    def test_foreign_hook_backed_up(self):
        foreign = "#!/bin/sh\necho husky post-commit\n"
        self.dest.write_text(foreign)
        il.install_post_commit(self.repo, POST_TEMPLATE)
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.backup.read_text(), foreign)
        self.assertIn(il.POST_COMMIT_MARKER, self.dest.read_text())

    def test_own_hook_overwritten_no_backup(self):
        self.dest.write_text(f"#!/bin/sh\n# {il.POST_COMMIT_MARKER}\necho old\n")
        il.install_post_commit(self.repo, POST_TEMPLATE)
        self.assertFalse(self.backup.exists())
        self.assertIn("merge_gate_post_commit", self.dest.read_text())

    def test_existing_backup_not_clobbered(self):
        self.backup.write_text("#!/bin/sh\necho ORIGINAL\n")
        self.dest.write_text("#!/bin/sh\necho some other foreign\n")
        il.install_post_commit(self.repo, POST_TEMPLATE)
        self.assertEqual(self.backup.read_text(), "#!/bin/sh\necho ORIGINAL\n")
        self.assertIn(il.POST_COMMIT_MARKER, self.dest.read_text())


class Test42StaleBackupNeverDiscardsForeignHook(unittest.TestCase):
    """#42 — a stale <hook>.pre-merge-gate must never cause the LIVE foreign
    hook to be silently discarded: the fresh backup escalates to the next free
    numbered name (.pre-merge-gate.<n>), byte- and mode-preserving, and
    uninstall restores the NEWEST backup (what was live just before install),
    leaving older stale backups untouched (operator content)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.hooks = self.repo / ".git" / "hooks"
        self.hooks.mkdir(parents=True)
        self.dest = self.hooks / "pre-push"
        self.backup = self.hooks / "pre-push.pre-merge-gate"

    def tearDown(self):
        self._tmp.cleanup()

    def test_repro_live_foreign_hook_preserved_despite_stale_backup(self):
        # AC1 reproduction: stale backup + live foreign hook → pre-fix the live
        # hook is overwritten with NO backup of its own.
        stale = "#!/bin/sh\necho OLD backup from a previous cycle\n"
        live = "#!/bin/sh\necho CURRENT husky hook\n"
        self.backup.write_text(stale)
        self.dest.write_text(live)
        os.chmod(self.dest, 0o700)
        il.install_pre_push(self.repo, TEMPLATE)
        # Stale backup untouched; live foreign content preserved SOMEWHERE.
        self.assertEqual(self.backup.read_text(), stale)
        escalated = self.hooks / "pre-push.pre-merge-gate.1"
        self.assertTrue(escalated.exists())
        self.assertEqual(escalated.read_text(), live)            # byte-preserving
        self.assertEqual(os.stat(escalated).st_mode & 0o777, 0o700)  # mode-preserving
        self.assertIn(il.PRE_PUSH_MARKER, self.dest.read_text())

    def test_second_stale_backup_escalates_to_next_number(self):
        self.backup.write_text("#!/bin/sh\necho stale 0\n")
        (self.hooks / "pre-push.pre-merge-gate.1").write_text("#!/bin/sh\necho stale 1\n")
        live = "#!/bin/sh\necho third foreign hook\n"
        self.dest.write_text(live)
        il.install_pre_push(self.repo, TEMPLATE)
        self.assertEqual((self.hooks / "pre-push.pre-merge-gate.2").read_text(), live)

    def test_gap_in_numbered_backups_stays_monotone(self):
        # A deleted middle backup must not make a NEW backup reuse a lower
        # number than an existing one (restore-newest would then pick wrong).
        self.backup.write_text("#!/bin/sh\necho stale 0\n")
        (self.hooks / "pre-push.pre-merge-gate.2").write_text("#!/bin/sh\necho stale 2\n")
        live = "#!/bin/sh\necho live\n"
        self.dest.write_text(live)
        il.install_pre_push(self.repo, TEMPLATE)
        self.assertEqual((self.hooks / "pre-push.pre-merge-gate.3").read_text(), live)

    def test_post_commit_symmetric(self):
        # AC5 — post-commit goes through the same _install_hook path.
        stale = "#!/bin/sh\necho OLD\n"
        live = "#!/bin/sh\necho CURRENT post-commit\n"
        (self.hooks / "post-commit.pre-merge-gate").write_text(stale)
        (self.hooks / "post-commit").write_text(live)
        il.install_post_commit(self.repo, POST_TEMPLATE)
        self.assertEqual((self.hooks / "post-commit.pre-merge-gate").read_text(), stale)
        self.assertEqual(
            (self.hooks / "post-commit.pre-merge-gate.1").read_text(), live)
        self.assertIn(il.POST_COMMIT_MARKER, (self.hooks / "post-commit").read_text())

    def test_identical_regenerated_hook_does_not_grow_backup_chain(self):
        # 2903e83 review, claude:finding-1 — a manager regenerating the SAME
        # foreign hook on every cycle (husky on npm install) must not add a
        # byte-identical backup per cycle; only content/mode changes do.
        regen = "#!/bin/sh\necho husky\n"
        for _ in range(3):
            self.dest.write_text(regen)          # manager clobbers our hook
            os.chmod(self.dest, 0o755)
            il.install_pre_push(self.repo, TEMPLATE)
        self.assertEqual(self.backup.read_text(), regen)
        self.assertFalse((self.hooks / "pre-push.pre-merge-gate.1").exists())
        # a CHANGED foreign hook still gets its own backup
        self.dest.write_text("#!/bin/sh\necho husky v2\n")
        il.install_pre_push(self.repo, TEMPLATE)
        self.assertEqual((self.hooks / "pre-push.pre-merge-gate.1").read_text(),
                         "#!/bin/sh\necho husky v2\n")

    def test_same_bytes_different_mode_still_backed_up(self):
        # mode is part of what restore preserves — dedup only on bytes+mode.
        self.backup.write_text("#!/bin/sh\necho husky\n")
        os.chmod(self.backup, 0o755)
        self.dest.write_text("#!/bin/sh\necho husky\n")
        os.chmod(self.dest, 0o700)
        il.install_pre_push(self.repo, TEMPLATE)
        escalated = self.hooks / "pre-push.pre-merge-gate.1"
        self.assertTrue(escalated.exists())
        self.assertEqual(os.stat(escalated).st_mode & 0o777, 0o700)

    def test_uninstall_restores_newest_backup_leaves_stale_one(self):
        # AC4 — after an escalated install, uninstall restores the hook that was
        # live just before install (the newest backup); the stale unsuffixed
        # backup is operator content and stays.
        stale = "#!/bin/sh\necho OLD backup\n"
        live = "#!/bin/sh\necho CURRENT husky hook\n"
        self.backup.write_text(stale)
        self.dest.write_text(live)
        os.chmod(self.dest, 0o700)
        il.install_pre_push(self.repo, TEMPLATE)
        il.uninstall(self.repo, self.repo / "settings.json")
        self.assertEqual(self.dest.read_text(), live)              # newest restored
        self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o700)
        self.assertFalse((self.hooks / "pre-push.pre-merge-gate.1").exists())  # consumed
        self.assertEqual(self.backup.read_text(), stale)            # stale kept


class TestSymlinkForeignHook(unittest.TestCase):
    """2903e83 review, codex:finding-0 — a SYMLINKED foreign hook must not be
    written through: write_text follows the link and corrupts the hook
    manager's target file, which uninstall cannot un-corrupt. The fix backs up
    the LINK itself by same-dir rename (relative targets keep resolving) and
    installs our hook as a fresh regular file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.hooks = self.repo / ".git" / "hooks"
        self.hooks.mkdir(parents=True)
        self.dest = self.hooks / "pre-push"
        self.backup = self.hooks / "pre-push.pre-merge-gate"
        # hook manager's own tree, linked RELATIVELY from .git/hooks
        self.target = self.repo / "hookmgr" / "pre-push.sh"
        self.target.parent.mkdir()
        self.target.write_text("#!/bin/sh\necho managed hook\n")
        os.chmod(self.target, 0o700)
        self.dest.symlink_to(Path("..") / ".." / "hookmgr" / "pre-push.sh")

    def tearDown(self):
        self._tmp.cleanup()

    def test_repro_symlink_target_not_corrupted(self):
        il.install_pre_push(self.repo, TEMPLATE)
        # Pre-fix: write_text followed the link — the manager's file became the
        # merge-gate template. Post-fix: target untouched, byte and mode.
        self.assertEqual(self.target.read_text(), "#!/bin/sh\necho managed hook\n")
        self.assertEqual(os.stat(self.target).st_mode & 0o777, 0o700)
        # dest is now OUR regular file, not a write through the old link.
        self.assertFalse(self.dest.is_symlink())
        self.assertIn(il.PRE_PUSH_MARKER, self.dest.read_text())
        # the backup is the moved LINK itself and still resolves (same dir).
        self.assertTrue(self.backup.is_symlink())
        self.assertEqual(self.backup.read_text(), "#!/bin/sh\necho managed hook\n")

    def test_uninstall_restores_the_link_itself(self):
        il.install_pre_push(self.repo, TEMPLATE)
        il.uninstall(self.repo, self.repo / "settings.json")
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(self.dest.read_text(), "#!/bin/sh\necho managed hook\n")
        self.assertEqual(os.stat(self.target).st_mode & 0o777, 0o700)
        self.assertFalse(self.backup.is_symlink() and False)  # backup consumed:
        self.assertFalse(os.path.lexists(self.backup))

    def test_broken_symlink_does_not_create_target(self):
        # write_text through a BROKEN link would create the missing target.
        self.dest.unlink()
        self.dest.symlink_to(Path("..") / ".." / "gone" / "nowhere.sh")
        il.install_pre_push(self.repo, TEMPLATE)
        self.assertFalse((self.repo / "gone" / "nowhere.sh").exists())
        self.assertFalse(self.dest.is_symlink())
        self.assertIn(il.PRE_PUSH_MARKER, self.dest.read_text())
        self.assertTrue(os.path.lexists(self.backup))  # the link, moved aside


class TestUninstall(unittest.TestCase):
    """#33 4e — --uninstall removes our pre-push + post-commit hooks (restoring a
    foreign backup when present) and deregisters the stale global hooks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.hooks = self.repo / ".git" / "hooks"
        self.hooks.mkdir(parents=True)
        self.settings = self.repo / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_uninstall_removes_our_hooks(self):
        il.install_pre_push(self.repo, TEMPLATE)
        il.install_post_commit(self.repo, POST_TEMPLATE)
        self.settings.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": il.SCHED_CMD}]}],
            "PostToolUse": [{"matcher": "Edit|Write",
                             "hooks": [{"type": "command", "command": il.MARK_CMD}]}],
        }}))
        il.uninstall(self.repo, self.settings)
        self.assertFalse((self.hooks / "pre-push").exists())
        self.assertFalse((self.hooks / "post-commit").exists())
        cmds = [h["command"] for grp in json.loads(self.settings.read_text())
                .get("hooks", {}).values() for g in grp for h in g["hooks"]]
        self.assertNotIn(il.SCHED_CMD, cmds)
        self.assertNotIn(il.MARK_CMD, cmds)

    def test_uninstall_restores_foreign_backup(self):
        foreign = "#!/bin/sh\necho husky post-commit\n"
        self.dest = self.hooks / "post-commit"
        self.dest.write_text(foreign)
        il.install_post_commit(self.repo, POST_TEMPLATE)  # backs foreign up
        il.uninstall(self.repo, self.settings)
        # foreign hook restored, backup consumed
        self.assertEqual((self.hooks / "post-commit").read_text(), foreign)
        self.assertFalse((self.hooks / "post-commit.pre-merge-gate").exists())

    def test_uninstall_leaves_foreign_hook_without_marker_alone(self):
        # A post-commit that is NOT ours (no marker, no backup) must be untouched.
        foreign = "#!/bin/sh\necho not ours\n"
        (self.hooks / "post-commit").write_text(foreign)
        il.uninstall(self.repo, self.settings)
        self.assertEqual((self.hooks / "post-commit").read_text(), foreign)


if __name__ == "__main__":
    unittest.main()
