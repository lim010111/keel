#!/usr/bin/env python3
"""Tests for setup_agents_md.py — the ADR-0031 §C migrate-retitle fix (#10).

Stdlib unittest only — pytest is not installed in this environment.
Run:  python3 scripts/test_setup_agents_md.py -v

scaffold-doctor#10 (MISC-agentsmd-h1): the State-2 migration (only CLAUDE.md →
move content into AGENTS.md) moved the content verbatim, leaving the boilerplate
`# CLAUDE.md` H1 title inside the new AGENTS.md. The leading filename-title must
be rewritten to `# AGENTS.md`; everything else preserved.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import setup_agents_md as sam  # noqa: E402


def git_repo(td: str) -> Path:
    """Init a throwaway git repo at td and return its resolved root."""
    root = Path(td).resolve()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def run_apply(target: Path):
    """Run plan() then the real apply_actions(); return the action kinds.
    (Actions are variable-length: ok/warn are 2-tuples, change/migrate carry an
    apply_fn — drive them through the module's own apply path, not a hand copy.)"""
    actions = []
    sam.plan(target, actions)
    sam.apply_actions(actions)
    return [entry[0] for entry in actions]


class TestMigrateRetitle(unittest.TestCase):
    def test_state2_rewrites_claude_h1_to_agents(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            body = "Project guidance.\n\n## Build\n\nrun make. CLAUDE.md is mentioned here.\n"
            (target / "CLAUDE.md").write_text("# CLAUDE.md\n\n" + body, encoding="utf-8")
            kinds = run_apply(target)
            self.assertIn("migrate", kinds)
            agents = (target / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(agents.splitlines()[0], "# AGENTS.md",
                             "the leading title must become # AGENTS.md")
            # The whole body (incl. an in-body 'CLAUDE.md' mention) is preserved;
            # only the title line changed.
            self.assertEqual(agents, "# AGENTS.md\n\n" + body)

    def test_state2_custom_title_is_preserved(self):
        # A CLAUDE.md with a real title (not the boilerplate filename) keeps it —
        # the retitle only fires for the literal `# CLAUDE.md` filename-title.
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / "CLAUDE.md").write_text("# My Project\n\nstuff\n", encoding="utf-8")
            run_apply(target)
            agents = (target / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(agents.splitlines()[0], "# My Project",
                             "a meaningful title must not be clobbered")


class TestOtherStatesUnaffected(unittest.TestCase):
    def test_state1_neither_file_is_a_plain_change_not_migrate(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            kinds = run_apply(target)
            self.assertNotIn("migrate", kinds, "creating from template is not a migrate")
            self.assertTrue((target / "AGENTS.md").exists())
            self.assertTrue((target / "CLAUDE.md").exists())

    def test_migration_is_idempotent_on_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / "CLAUDE.md").write_text("# CLAUDE.md\n\nbody\n", encoding="utf-8")
            run_apply(target)  # migrate
            agents_after_first = (target / "AGENTS.md").read_text(encoding="utf-8")
            kinds = run_apply(target)  # re-run: now both exist + CLAUDE imports AGENTS
            self.assertNotIn("migrate", kinds, "a second run must not re-migrate")
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"),
                             agents_after_first, "re-run must not rewrite AGENTS.md")


class TestDiscovery(unittest.TestCase):
    def test_discovers_nested_guidance_dirs_and_always_the_sweep_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = git_repo(td)
            (root / "src").mkdir()
            (root / "gas").mkdir()
            (root / "src" / "CLAUDE.md").write_text("# CLAUDE.md\n", encoding="utf-8")
            (root / "gas" / "AGENTS.md").write_text("# AGENTS.md\n", encoding="utf-8")
            (root / "src" / "index.ts").write_text("x\n", encoding="utf-8")  # not guidance
            targets = sam.discover_targets(root, root)
            self.assertIn(root, targets, "sweep root is always a target")
            self.assertIn(root / "src", targets)
            self.assertIn(root / "gas", targets)
            self.assertNotIn(root / "src" / "index.ts", targets)

    def test_gitignored_dir_is_not_discovered(self):
        with tempfile.TemporaryDirectory() as td:
            root = git_repo(td)
            (root / ".gitignore").write_text("vendor/\n", encoding="utf-8")
            (root / "vendor").mkdir()
            (root / "vendor" / "CLAUDE.md").write_text("# CLAUDE.md\n", encoding="utf-8")
            self.assertNotIn(root / "vendor", sam.discover_targets(root, root))


class TestWiredSet(unittest.TestCase):
    def test_warn_and_error_dirs_are_excluded(self):
        results = [
            (Path("/a"), [("migrate", "m", None)]),
            (Path("/b"), [("ok", "o"), ("ok", "o")]),
            (Path("/c"), [("warn", "conflict")]),
            (Path("/d"), [("error", "broken")]),
        ]
        wired = sam.wired_dirs_after(results)
        self.assertEqual(wired, {Path("/a"), Path("/b")},
                         "only conflict-free, content-bearing dirs are wired")


class TestCrosslinkRewrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "gas").mkdir()
        self.wired = {self.root, self.root / "src", self.root / "gas"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_flips_link_target_text_and_inline_code_to_wired_dirs(self):
        text = ("[../gas/CLAUDE.md](../gas/CLAUDE.md) and "
                "[`../gas/CLAUDE.md`](../gas/CLAUDE.md) and `../gas/CLAUDE.md`")
        out, n = sam.rewrite_text_crosslinks(text, self.root / "src", self.wired)
        self.assertNotIn("CLAUDE.md", out, "every path-shaped ref flips")
        self.assertEqual(out.count("AGENTS.md"), 5)
        self.assertEqual(n, 5)

    def test_bare_prose_noun_is_preserved(self):
        text = "fix broken CLAUDE.md / README.md path refs"
        out, n = sam.rewrite_text_crosslinks(text, self.root, self.wired)
        self.assertEqual(out, text, "a bare-word CLAUDE.md noun must not change")
        self.assertEqual(n, 0)

    def test_ref_to_non_wired_dir_is_left_alone(self):
        # A link to a dir NOT in the wired set (e.g. a State-4 conflict, whose
        # CLAUDE.md keeps independent content) must not be flipped.
        text = "[../conflict/CLAUDE.md](../conflict/CLAUDE.md)"
        out, n = sam.rewrite_text_crosslinks(text, self.root / "src", self.wired)
        self.assertEqual(out, text)
        self.assertEqual(n, 0)

    def test_self_dir_link_flips(self):
        out, n = sam.rewrite_text_crosslinks("[CLAUDE.md](CLAUDE.md)",
                                             self.root / "src", self.wired)
        self.assertEqual(out, "[AGENTS.md](AGENTS.md)")
        self.assertEqual(n, 2)


class TestExternalReport(unittest.TestCase):
    def test_reports_file_relative_and_root_relative_refs_only_for_wired(self):
        with tempfile.TemporaryDirectory() as td:
            root = git_repo(td)
            (root / "src").mkdir()
            (root / "src" / "CLAUDE.md").write_text("# CLAUDE.md\n", encoding="utf-8")
            # root-relative ref inside a source comment under src/
            (root / "src" / "index.ts").write_text(
                '// see src/CLAUDE.md "rules"\n', encoding="utf-8")
            # file-relative markdown ref in a README
            (root / "README.md").write_text(
                "[`src/CLAUDE.md`](src/CLAUDE.md)\n", encoding="utf-8")
            # a ref to a NON-wired dir must not be reported
            (root / "other.md").write_text("see vendor/CLAUDE.md\n", encoding="utf-8")
            wired = {root / "src"}
            hits = sam.external_refs(root, root, wired)
            files = {rel for rel, _, _ in hits}
            self.assertIn("src/index.ts", files, "root-relative comment ref caught")
            self.assertIn("README.md", files, "file-relative markdown ref caught")
            self.assertNotIn("other.md", files, "ref to a non-wired dir is ignored")

    def test_agents_md_is_not_reported_as_external(self):
        with tempfile.TemporaryDirectory() as td:
            root = git_repo(td)
            (root / "src").mkdir()
            (root / "src" / "CLAUDE.md").write_text("# CLAUDE.md\n", encoding="utf-8")
            (root / "AGENTS.md").write_text(
                "[src/CLAUDE.md](src/CLAUDE.md)\n", encoding="utf-8")
            hits = sam.external_refs(root, root, {root / "src"})
            self.assertEqual(hits, [], "the AGENTS.md graph is auto-fixed, not reported")


if __name__ == "__main__":
    unittest.main(verbosity=2)
