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

import tempfile
import unittest
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import setup_agents_md as sam  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
