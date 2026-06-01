#!/usr/bin/env python3
"""Regression suite for the `Decide check outcome` step of codex-review.yml.

Exercises the gate decision logic against synthetic `validators.json` +
`codex-review.normalized.json` fixtures, with a focus on the fallback
fail-closed contract introduced for claude-harness-work#24.

The step's shell is extracted *dynamically* from the workflow template via
PyYAML — keeping the test and the workflow byte-coupled, so a future edit
to the step can't silently break this contract without the test
re-discovering the new shell.

Stdlib `unittest` + PyYAML only. Run:
    python3 scripts/test_decide_check_outcome.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
TEMPLATE = SCRIPTS.parent / "templates" / "codex-review.yml"


def _extract_decide_step_run() -> str:
    """Pull the `run:` body of the `Decide check outcome` step from the YAML."""
    doc = yaml.safe_load(TEMPLATE.read_text())
    steps = doc["jobs"]["codex-review"]["steps"]
    for step in steps:
        if step.get("name") == "Decide check outcome":
            return step["run"]
    raise RuntimeError(
        f"`Decide check outcome` step not found in {TEMPLATE}; the workflow "
        "shape may have changed — update this test."
    )


DECIDE_SHELL = _extract_decide_step_run()


def _write_fixtures(
    cwd: Path,
    *,
    validators: dict | None,
    normalized: dict | None,
) -> None:
    """Materialise the two artefact files the step reads."""
    review_dir = cwd / ".codex-review"
    review_dir.mkdir(parents=True, exist_ok=True)
    if validators is not None:
        (review_dir / "validators.json").write_text(json.dumps(validators))
    if normalized is not None:
        (review_dir / "codex-review.normalized.json").write_text(json.dumps(normalized))


def _run_step(cwd: Path, *, soft_mode: bool) -> subprocess.CompletedProcess:
    """Execute the extracted shell under bash with controlled env."""
    env = os.environ.copy()
    env["SOFT_MODE"] = "true" if soft_mode else "false"
    return subprocess.run(
        ["bash", "-c", DECIDE_SHELL],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


CRITICAL_HIGH_NORMALIZED = {
    "result": {
        "verdict": "needs_changes",
        "summary": "synthetic — one high finding",
        "findings": [
            {
                "severity": "high",
                "title": "Synthetic high finding",
                "file": "synthetic.py",
                "line": 1,
            }
        ],
        "next_steps": [],
    },
    "codex": {"status": "ok", "exit": 0},
}

CLEAN_NORMALIZED = {
    "result": {
        "verdict": "ok",
        "summary": "synthetic — no findings",
        "findings": [],
        "next_steps": [],
    },
    "codex": {"status": "ok", "exit": 0},
}

VALIDATORS_FALLBACK = {
    "validators": [],
    "aggregate": [],
    "fallback": "synthetic-test-reason",
}

VALIDATORS_CLEAN = {
    "validators": [{"name": "claude", "verdict": "ok"}],
    "aggregate": [],
    "fallback": "",
}


class TestDecideCheckOutcomeFallback(unittest.TestCase):
    """claude-harness-work#24 — validator fallback fail-closed contract."""

    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp(prefix="decide-outcome-test-"))

    def tearDown(self):
        shutil.rmtree(self.cwd, ignore_errors=True)

    def test_hard_mode_fallback_with_critical_high_fails_closed(self):
        """Issue AC: fallback set + critical/high present → exit 1."""
        _write_fixtures(
            self.cwd,
            validators=VALIDATORS_FALLBACK,
            normalized=CRITICAL_HIGH_NORMALIZED,
        )
        r = _run_step(self.cwd, soft_mode=False)
        self.assertEqual(
            r.returncode, 1,
            f"expected exit 1; got {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}",
        )
        # The error annotation names the threat explicitly.
        self.assertIn("Validator fallback (fail-closed)", r.stdout + r.stderr)
        self.assertIn("synthetic-test-reason", r.stdout + r.stderr)

    def test_hard_mode_fallback_with_no_critical_high_continues_with_warning(self):
        """Issue AC: fallback set + no critical/high → exit 0 + benign warning."""
        _write_fixtures(
            self.cwd,
            validators=VALIDATORS_FALLBACK,
            normalized=CLEAN_NORMALIZED,
        )
        r = _run_step(self.cwd, soft_mode=False)
        self.assertEqual(
            r.returncode, 0,
            f"expected exit 0; got {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}",
        )
        self.assertIn("Validator fallback (benign)", r.stdout + r.stderr)
        # And the gate-passed message still fires (no blockers).
        self.assertIn("gate passed", r.stdout + r.stderr)

    def test_hard_mode_no_fallback_clean_pass(self):
        """Existing behaviour preserved: no fallback + no blockers → exit 0."""
        _write_fixtures(
            self.cwd,
            validators=VALIDATORS_CLEAN,
            normalized=CLEAN_NORMALIZED,
        )
        r = _run_step(self.cwd, soft_mode=False)
        self.assertEqual(
            r.returncode, 0,
            f"expected exit 0; got {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}",
        )
        self.assertIn("gate passed", r.stdout + r.stderr)
        # The fallback branches must not fire on a clean run.
        self.assertNotIn("Validator fallback", r.stdout + r.stderr)


class TestDecideCheckOutcomeRegressionGuards(unittest.TestCase):
    """Pre-existing branches must still work after the #24 insertion."""

    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp(prefix="decide-outcome-test-"))

    def tearDown(self):
        shutil.rmtree(self.cwd, ignore_errors=True)

    def test_hard_mode_missing_validators_json_fails_closed(self):
        """Validator infra-failure branch (validators.json absent) → exit 1."""
        _write_fixtures(self.cwd, validators=None, normalized=CRITICAL_HIGH_NORMALIZED)
        r = _run_step(self.cwd, soft_mode=False)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("Validator infra failure", r.stdout + r.stderr)

    def test_hard_mode_codex_failed_status_fails_closed(self):
        """Codex-side failure (codex.status != 'ok') → exit 1."""
        normalized = dict(CRITICAL_HIGH_NORMALIZED)
        normalized["codex"] = {"status": "codex-failed", "exit": 2}
        _write_fixtures(self.cwd, validators=VALIDATORS_CLEAN, normalized=normalized)
        r = _run_step(self.cwd, soft_mode=False)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("Codex failed", r.stdout + r.stderr)

    def test_soft_mode_never_blocks_even_with_fallback_and_critical_high(self):
        """Soft mode: report-only, must NOT block on fallback path."""
        _write_fixtures(
            self.cwd,
            validators=VALIDATORS_FALLBACK,
            normalized=CRITICAL_HIGH_NORMALIZED,
        )
        r = _run_step(self.cwd, soft_mode=True)
        self.assertEqual(
            r.returncode, 0,
            f"soft mode must never block; got exit {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}",
        )
        self.assertIn("Soft mode", r.stdout + r.stderr)


if __name__ == "__main__":
    if not TEMPLATE.exists():
        raise SystemExit(f"workflow template not found at {TEMPLATE}")
    if shutil.which("jq") is None:
        raise SystemExit("`jq` not on $PATH — install jq to run these tests.")
    unittest.main(verbosity=2)
