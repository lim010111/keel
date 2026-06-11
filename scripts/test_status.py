#!/usr/bin/env python3
"""Tests for status.py's completion-label lint + --brief view
(claude-harness-work status-harness#03).

Stdlib unittest only — pytest is not installed in this environment.
Run:  python3 scripts/test_status.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import status  # noqa: E402

STATUS_PY = SCRIPTS / "status.py"


def git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def seed_issue(root: Path, feature: str, nn: str, slug: str, *, total: int = 2,
               done: int = 0, status_line: str = "ready-for-agent") -> Path:
    d = root / ".scratch" / feature / "issues"
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# {slug.replace('-', ' ').title()}", "",
             f"Status: {status_line}", "", "## Acceptance criteria", ""]
    for i in range(total):
        box = "x" if i < done else " "
        lines.append(f"- [{box}] criterion {i + 1}")
    lines += ["", "## Blocked by", "", "None — can start immediately."]
    p = d / f"{nn}-{slug}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def run_status(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", str(STATUS_PY), *args], cwd=str(root),
                          capture_output=True, text=True, timeout=60)


# --------------------------------------------------------------------------
# completion_offenders — denylist anchored at the bold bullet-label position
# --------------------------------------------------------------------------
class TestCompletionOffenders(unittest.TestCase):
    def test_observed_offender_shapes_fire(self):
        # The exact shapes that accumulated in the real STATUS.md narrative.
        for line in [
            "- **완료** = merge-gate **#40 done** (per-review findings archive)",
            "- **완료 · merge-gate #41:** Layer-2 측정 wrapper 은퇴",
            "- **완료(phase-1)** = scaffold-doctor",
            "- **종료 · #31 measurement:** 승급 DECLINED",
            "- **하드닝-done · publication-gate#02 (a2):** shipped + active",
            "- **완료된 트랙** — 한국어 라벨은 prefix-match",
            "- **Done · feat #9:** English label, word-bounded",
            "- **shipped · feat #9:** lowercase english",
        ]:
            self.assertEqual(status.completion_offenders(line), [line],
                             f"must fire: {line}")

    def test_legitimate_lines_do_not_fire(self):
        for line in [
            "- **능동 · merge-gate #43:** rename 작업",
            "- **병행 · self-verify#01:** activation 이슈 신설",
            "- **휴면 · GHA 프로필:** frozen — 손대지 않음",
            "- **#25 trust-boundary mitigation (ADR-0007 미작성)** — open decision",
            "- **post-v1 hardening 백로그** — 2nd validator",
            "상세는 #41 Resolution 에 완료 기록이 있다.",   # prose mention
            "  - **완료** = indented continuation, not top-level",
            "- **donelike-word** is not word-bounded done",  # 'done' prefix of a longer word
        ]:
            self.assertEqual(status.completion_offenders(line), [],
                             f"must NOT fire: {line}")

    def test_warning_counts_and_clean_is_empty(self):
        dirty = ("## Start here next session\n\n"
                 "- **완료 · a:** x\n- **능동 · b:** y\n- **종료 · c:** z\n")
        w = status.completion_warning(dirty)
        self.assertIn("(2)", w)
        self.assertIn("Completed-track", w)
        self.assertEqual(status.completion_warning("- **능동 · b:** y\n"), "")


# --------------------------------------------------------------------------
# --brief — file unchanged-contract + actionable-rows-only stdout
# --------------------------------------------------------------------------
class TestBrief(unittest.TestCase):
    def _repo(self, td: str) -> Path:
        root = Path(td)
        git_init(root)
        seed_issue(root, "feat", "01", "alpha-done", total=2, done=2)
        seed_issue(root, "feat", "02", "bravo-todo", total=3, done=0)
        seed_issue(root, "feat", "03", "carol-parked", total=2, done=0,
                   status_line="parked")
        return root

    def test_brief_filters_rows_and_keeps_file_complete(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            r = run_status(root, "--brief")
            self.assertEqual(r.returncode, 0, r.stderr)
            out = r.stdout
            self.assertIn("Bravo Todo", out, "actionable row must appear")
            self.assertNotIn("Alpha Done", out, "done row must be collapsed")
            self.assertNotIn("Carol Parked", out, "parked row must be collapsed")
            self.assertIn("1 done + 1 parked omitted", out)
            self.assertIn("Current focus", out, "narrative must be injected")
            self.assertNotIn("STATUS.md regenerated", out,
                             "regen message must not pollute the injection")
            full = (root / "STATUS.md").read_text(encoding="utf-8")
            self.assertIn("Alpha Done", full)
            self.assertIn("Carol Parked", full)

    def test_brief_writes_byte_identical_file(self):
        with tempfile.TemporaryDirectory() as p, tempfile.TemporaryDirectory() as q:
            rootP, rootQ = self._repo(p), self._repo(q)
            self.assertEqual(run_status(rootP).returncode, 0)
            self.assertEqual(run_status(rootQ, "--brief").returncode, 0)
            self.assertEqual((rootP / "STATUS.md").read_text(encoding="utf-8"),
                             (rootQ / "STATUS.md").read_text(encoding="utf-8"),
                             "--brief must regenerate the file exactly as a plain run")

    def test_brief_silent_outside_issue_projects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            r = run_status(root, "--brief")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "", "non-issue repo must stay silent")

    def test_brief_surfaces_completion_banner(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            run_status(root)  # generate, then dirty the narrative
            text = (root / "STATUS.md").read_text(encoding="utf-8")
            dirty = text.replace(
                status.NARRATIVE_START + "\n",
                status.NARRATIVE_START + "\n- **완료 · feat #01:** alpha shipped\n")
            (root / "STATUS.md").write_text(dirty, encoding="utf-8")
            r = run_status(root, "--brief")
            self.assertIn("Completed-track lines", r.stdout,
                          "advisory banner must reach the injected view")


if __name__ == "__main__":
    unittest.main(verbosity=2)
