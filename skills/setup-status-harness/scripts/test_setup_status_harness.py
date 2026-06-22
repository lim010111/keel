#!/usr/bin/env python3
"""Tests for setup_status_harness.py — the two ADR-0031 §C supported-path fixes.

Stdlib unittest only — pytest is not installed in this environment.
Run:  python3 scripts/test_setup_status_harness.py -v

status-harness#05 — the generated regen-status workflow's commit gate must
commit the FIRST (untracked) STATUS.md, not just an already-tracked change.
status-harness#06 — the installer must emit the `--brief` SessionStart variant
and upgrade a bare `status.py` hook in place rather than leaving it stale.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import setup_status_harness as sh  # noqa: E402


# --------------------------------------------------------------------------
# #05 — workflow commit gate, exercised against the ACTUAL shipped YAML
# --------------------------------------------------------------------------
def extract_commit_script(workflow_text: str) -> str:
    """Pull the dedented `run: |` block of the 'Commit if changed' step out of
    the shipped WORKFLOW_CONTENT, so the test runs the real gate, not a copy."""
    lines = workflow_text.splitlines()
    idx = next(i for i, l in enumerate(lines) if "Commit if changed" in l)
    run_idx = next(i for i in range(idx, len(lines)) if lines[i].strip() == "run: |")
    body, base = [], None
    for l in lines[run_idx + 1:]:
        if l.strip() == "":
            body.append("")
            continue
        indent = len(l) - len(l.lstrip())
        if base is None:
            base = indent
        if indent < base:
            break
        body.append(l[base:])
    return "\n".join(body)


def _git(args, cwd, **kw):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, **kw)


def repo_with_origin(td: str) -> Path:
    """A git repo with an initial commit on main and a bare origin it tracks —
    the state the regen workflow runs in (checked-out main, pushable)."""
    root = Path(td) / "repo"
    root.mkdir()
    bare = Path(td) / "origin.git"
    _git(["init", "--bare", "-b", "main", str(bare)], td, check=True)
    _git(["init", "-b", "main", str(root)], td, check=True)
    _git(["config", "user.email", "t@t"], root, check=True)
    _git(["config", "user.name", "t"], root, check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], root, check=True)
    _git(["commit", "-m", "seed"], root, check=True)
    _git(["remote", "add", "origin", str(bare)], root, check=True)
    _git(["push", "-u", "origin", "main"], root, check=True)
    return root


def run_gate(root: Path):
    script = extract_commit_script(sh.WORKFLOW_CONTENT)
    return subprocess.run(["bash", "-c", script], cwd=str(root),
                          capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t",
                               "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t"})


def commit_count(root: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], root, check=True).stdout.strip())


class TestWorkflowCommitGate(unittest.TestCase):
    def test_untracked_first_status_is_committed(self):
        with tempfile.TemporaryDirectory() as td:
            root = repo_with_origin(td)
            before = commit_count(root)
            # status.py just generated the FIRST STATUS.md — untracked.
            (root / "STATUS.md").write_text("# board\n", encoding="utf-8")
            r = run_gate(root)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(commit_count(root), before + 1,
                             "the first (untracked) STATUS.md must be committed")
            log = _git(["log", "-1", "--pretty=%s"], root, check=True).stdout
            self.assertIn("Regenerate STATUS.md", log)

    def test_tracked_unchanged_makes_no_empty_commit(self):
        with tempfile.TemporaryDirectory() as td:
            root = repo_with_origin(td)
            (root / "STATUS.md").write_text("# board\n", encoding="utf-8")
            _git(["add", "STATUS.md"], root, check=True)
            _git(["commit", "-m", "add status"], root, check=True)
            before = commit_count(root)
            # Re-run with STATUS.md tracked and byte-unchanged.
            r = run_gate(root)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(commit_count(root), before,
                             "an unchanged STATUS.md must not create an empty commit")


# --------------------------------------------------------------------------
# #06 — SessionStart hook is the --brief variant; bare hooks upgrade in place
# --------------------------------------------------------------------------
class TestSessionStartHook(unittest.TestCase):
    def test_command_is_brief_and_matches_live_settings(self):
        self.assertIn("--brief", sh.SESSIONSTART_CMD,
                      "installer must emit the lightweight --brief variant")
        live = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        live_ss = [h["command"]
                   for e in live["hooks"].get("SessionStart", [])
                   for h in e.get("hooks", [])
                   if "status.py" in h.get("command", "")]
        self.assertIn(sh.SESSIONSTART_CMD, live_ss,
                      "installer SESSIONSTART_CMD must match the live settings.json form")

    def test_fresh_install_adds_brief_hook(self):
        ss = []
        kind, _msg, changed = sh.ensure_sessionstart_hook(ss)
        self.assertEqual((kind, changed), ("change", True))
        self.assertEqual(len(ss), 1)
        self.assertIn("--brief", ss[0]["hooks"][0]["command"])

    def test_bare_hook_upgraded_in_place(self):
        ss = [{"hooks": [{"type": "command",
                          "command": 'python3 "$HOME/.claude/scripts/status.py"'}]}]
        kind, _msg, changed = sh.ensure_sessionstart_hook(ss)
        self.assertEqual((kind, changed), ("change", True))
        self.assertEqual(len(ss), 1, "a bare hook must be upgraded, not duplicated")
        self.assertIn("--brief", ss[0]["hooks"][0]["command"])

    def test_idempotent_when_brief_present(self):
        ss = []
        sh.ensure_sessionstart_hook(ss)          # install
        snapshot = json.dumps(ss)
        kind, _msg, changed = sh.ensure_sessionstart_hook(ss)  # re-run
        self.assertEqual((kind, changed), ("ok", False))
        self.assertEqual(json.dumps(ss), snapshot, "second run must not change the hook")


if __name__ == "__main__":
    unittest.main(verbosity=2)
