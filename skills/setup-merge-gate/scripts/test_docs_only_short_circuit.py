#!/usr/bin/env python3
"""Regression suite for the docs-only short-circuit + trust-document override
in codex-review.yml's preflight job (claude-harness-work#27).

The docs-only fast path skips the heavy gate when every changed file is
prose. #27 adds a TRUST_DOC_GLOBS *negative* override: a PR that touches the
validator's own trust/context inputs (AGENTS.md, the CLAUDE.md files that
@import it, CONTEXT-MAP.md, ADRs) must run the full gate even if every changed
file is otherwise docs-only — those files are the gate's calibration and must
not be weakened unreviewed.

Like test_decide_check_outcome.py, the step's shell is extracted *dynamically*
from the workflow template via PyYAML, so the test stays byte-coupled to the
actual workflow: a future edit to the step can't silently break this contract
without the test re-discovering the new shell.

Stdlib unittest + PyYAML + git. Run:
    python3 scripts/test_docs_only_short_circuit.py -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
TEMPLATE = SCRIPTS.parent / "templates" / "codex-review.yml"

# The defaults render.py / SKILL.md ship.
DOCS_ONLY_GLOBS = '["**/*.md","docs/**","LICENSE","NOTICE"]'
TRUST_DOC_GLOBS = (
    '["AGENTS.md","**/AGENTS.md","CLAUDE.md","**/CLAUDE.md",'
    '"CONTEXT-MAP.md","**/CONTEXT.md","docs/adr/**"]'
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _extract_docs_step_run() -> str:
    """Pull the `run:` body of the `Docs-only short-circuit` step from the YAML."""
    doc = yaml.safe_load(TEMPLATE.read_text())
    for step in doc["jobs"]["preflight"]["steps"]:
        if step.get("name") == "Docs-only short-circuit":
            return step["run"]
    raise RuntimeError(
        f"`Docs-only short-circuit` step not found in {TEMPLATE}; the workflow "
        "shape may have changed — update this test."
    )


DOCS_SHELL = _extract_docs_step_run()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


class _DocsStepRunner:
    """A temp git repo whose HEAD commit changes a given file set; runs the
    extracted docs-only step against it and returns the docs_only output."""

    def __init__(self):
        self.cwd = Path(tempfile.mkdtemp(prefix="docs-only-test-"))
        _git(self.cwd, "init", "-q")
        (self.cwd / ".keep").write_text("base\n")
        _git(self.cwd, "add", ".keep")
        _git(self.cwd, "commit", "-q", "-m", "base")
        self.base_sha = _git(self.cwd, "rev-parse", "HEAD")

    def run(self, changed_files, *, docs_globs=DOCS_ONLY_GLOBS, trust_globs=TRUST_DOC_GLOBS):
        for rel in changed_files:
            p = self.cwd / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("change\n")
            _git(self.cwd, "add", rel)
        _git(self.cwd, "commit", "-q", "-m", "head")
        head_sha = _git(self.cwd, "rev-parse", "HEAD")

        out_file = self.cwd / "gh_output"
        out_file.write_text("")
        env = {
            **os.environ,
            "DOCS_ONLY_GLOBS": docs_globs,
            "TRUST_DOC_GLOBS": trust_globs,
            "BASE_SHA": self.base_sha,
            "HEAD_SHA": head_sha,
            "GITHUB_OUTPUT": str(out_file),
        }
        proc = subprocess.run(
            ["bash", "-c", DOCS_SHELL],
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (
            f"docs-only step exited {proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
        value = None
        for line in out_file.read_text().splitlines():
            if line.startswith("docs_only="):
                value = line.split("=", 1)[1]
        assert value is not None, (
            f"no docs_only written to GITHUB_OUTPUT\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
        return value, proc

    def cleanup(self):
        shutil.rmtree(self.cwd, ignore_errors=True)


class TestDocsOnlyTrustOverride(unittest.TestCase):
    """claude-harness-work#27 — trust-document negative override."""

    def setUp(self):
        self.runner = _DocsStepRunner()

    def tearDown(self):
        self.runner.cleanup()

    def test_all_prose_short_circuits(self):
        """Pure prose PR → docs_only=true (fast path)."""
        v, _ = self.runner.run(["README.md", "CHANGELOG.md"])
        self.assertEqual(v, "true")

    def test_regular_doc_short_circuits(self):
        """A non-trust doc under docs/ still short-circuits."""
        v, _ = self.runner.run(["docs/marketplace-readiness.md"])
        self.assertEqual(v, "true")

    def test_agents_md_forces_gate(self):
        """Editing AGENTS.md alone forces the full gate."""
        v, _ = self.runner.run(["AGENTS.md"])
        self.assertEqual(v, "false")

    def test_adr_forces_gate(self):
        """Appending/altering an ADR forces the full gate (docs/adr/**)."""
        v, _ = self.runner.run(["docs/adr/0006-foo.md"])
        self.assertEqual(v, "false")

    def test_module_claude_md_forces_gate(self):
        """A per-module CLAUDE.md is live agent context — must not skip."""
        v, _ = self.runner.run(["src/mod/CLAUDE.md"])
        self.assertEqual(v, "false")

    def test_trust_override_beats_docs_match(self):
        """README.md (prose) + AGENTS.md (trust) → trust override wins."""
        v, _ = self.runner.run(["README.md", "AGENTS.md"])
        self.assertEqual(v, "false")

    def test_non_docs_file_is_not_docs_only(self):
        """A code change is not docs-only regardless of the trust set."""
        v, _ = self.runner.run(["src/app.py"])
        self.assertEqual(v, "false")

    def test_empty_trust_set_disables_override(self):
        """An empty TRUST_DOC_GLOBS opts out — AGENTS.md short-circuits again."""
        v, _ = self.runner.run(["AGENTS.md"], trust_globs="[]")
        self.assertEqual(v, "true")

    def test_malformed_trust_set_fails_safe_to_gate(self):
        """A malformed trust set biases toward running the gate, with a warning."""
        v, proc = self.runner.run(["README.md"], trust_globs="not-json")
        self.assertEqual(v, "false")
        self.assertIn("TRUST_DOC_GLOBS", proc.stderr)


if __name__ == "__main__":
    if not TEMPLATE.exists():
        raise SystemExit(f"workflow template not found at {TEMPLATE}")
    if shutil.which("git") is None:
        raise SystemExit("`git` not on $PATH — required for these tests.")
    unittest.main(verbosity=2)
