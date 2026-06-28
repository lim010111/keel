#!/usr/bin/env python3
"""Tests for status.py's completion-label lint + --brief view
(claude-harness-work status-harness#03).

Stdlib unittest only — pytest is not installed in this environment.
Run:  python3 scripts/test_status.py -v
"""
from __future__ import annotations

import inspect
import os
import re
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
            "- **병행 · keel mirror:** lockstep — 미러 편집과 같은 세션",
            "- **휴면 · publication-gate:** #03 parked — 손대지 않음",
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


# --------------------------------------------------------------------------
# next_pointer — AC3: the card next-action is computed from issue lifecycle
# state, NEVER parsed from the narrative (status-harness#07).
# --------------------------------------------------------------------------
class TestNextPointer(unittest.TestCase):
    @staticmethod
    def _issue(num: str, done: int, total: int, *, triage="ready-for-agent",
               blockers=None) -> dict:
        return {"num": num, "title": f"issue {num}", "triage": triage,
                "done": done, "total": total, "blockers": blockers or []}

    def _resolve(self, issues):
        by_num = {i["num"]: i for i in issues}
        states = {i["num"]: status.lifecycle(i, by_num) for i in issues}
        return status.next_pointer(issues, states)

    def test_in_progress_wins_over_todo(self):
        nxt = self._resolve([self._issue("01", 1, 3), self._issue("02", 0, 2)])
        self.assertEqual(nxt["num"], "01")

    def test_lowest_numbered_todo(self):
        nxt = self._resolve([self._issue("05", 0, 2), self._issue("02", 0, 2)])
        self.assertEqual(nxt["num"], "02")

    def test_blocked_is_skipped(self):
        # #01 is blocked by the unfinished #02; #02 is a plain todo. Even though
        # #01 sorts lowest, blocked is never pointed at — the todo wins.
        nxt = self._resolve([self._issue("01", 0, 2, blockers=["02"]),
                             self._issue("02", 0, 2)])
        self.assertEqual(nxt["num"], "02")

    def test_all_done_is_none(self):
        self.assertIsNone(
            self._resolve([self._issue("01", 2, 2), self._issue("02", 3, 3)]))

    def test_dormant_parked_wontfix_is_none(self):
        self.assertIsNone(
            self._resolve([self._issue("01", 0, 2, triage="parked"),
                           self._issue("02", 0, 2, triage="wontfix")]))

    def test_source_never_reads_the_narrative(self):
        # The structural guard behind AC3/AC7: a future change that "fixes" a
        # perceived card/narrative mismatch by sourcing the pointer from prose
        # is a regression. Pin it at the source level. The docstring legitimately
        # documents the invariant ("never the narrative"), so grep the BODY only.
        sig = inspect.signature(status.next_pointer)
        self.assertEqual(list(sig.parameters), ["issues", "states"],
                         "pointer must derive only from issue state")
        src = inspect.getsource(status.next_pointer)
        body = re.sub(r'""".*?"""', "", src, count=1, flags=re.S).lower()
        self.assertNotIn("narrative", body)


# --------------------------------------------------------------------------
# md_to_html — AC4: stdlib-only hand-rolled converter, no external dependency.
# --------------------------------------------------------------------------
class TestMdToHtml(unittest.TestCase):
    def test_core_constructs(self):
        html = status.md_to_html(
            "## Current focus\n\nA **bold** word, `code`, a [link](http://x).\n\n"
            "- item one\n- item two\n")
        self.assertIn("<h2>Current focus</h2>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<code>code</code>", html)
        self.assertIn('<a href="http://x">link</a>', html)
        self.assertIn("<li>item one</li>", html)
        self.assertNotIn("**", html)
        self.assertNotIn("## ", html)

    def test_strips_comments_and_renders_table(self):
        html = status.md_to_html(
            "<!-- secret comment -->\n## H\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
        self.assertNotIn("secret comment", html)
        self.assertIn("<table>", html)
        self.assertIn("<th>A</th>", html)
        self.assertIn("<td>1</td>", html)

    def test_escapes_dangerous_text(self):
        # Raw angle brackets in narrative prose must not become live markup.
        html = status.md_to_html("a < b & c > d\n")
        self.assertNotIn("<b ", html)
        self.assertIn("&lt;", html)
        self.assertIn("&amp;", html)

    def test_no_external_markdown_dependency(self):
        src = (SCRIPTS / "status.py").read_text(encoding="utf-8")
        for bad in ("import markdown", "markdown_it", "markdown-it", "mistune"):
            self.assertNotIn(bad, src, f"stdlib-only: {bad} must be absent")


# --------------------------------------------------------------------------
# --html end-to-end — AC1/AC2/AC5/AC6 file-lifecycle contract.
# --------------------------------------------------------------------------
class TestHtmlEndToEnd(unittest.TestCase):
    def _run_html(self, root: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "STATUS_HTML_NO_OPEN": "1"}
        return subprocess.run(["python3", str(STATUS_PY), "--html"], cwd=str(root),
                              capture_output=True, text=True, timeout=60, env=env)

    def test_no_op_without_issue_files(self):  # AC1
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            r = self._run_html(root)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((root / "STATUS.html").exists(),
                             "no issues → silent no-op, no HTML written")

    def test_emits_dashboard_with_computed_pointer(self):  # AC1/AC2/AC3/AC5
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "05", "echo-todo", total=2, done=0)
            seed_issue(root, "feat", "02", "bravo-todo", total=2, done=0)
            r = self._run_html(root)
            self.assertEqual(r.returncode, 0, r.stderr)
            text = (root / "STATUS.html").read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE html>", text)
            self.assertIn("<details>", text, "AC5 — collapsible drill-down")
            # AC2/AC3 — lowest-numbered todo (#02) is the computed pointer.
            self.assertRegex(text, r'next-num">#02<')
            self.assertIn("Bravo Todo", text)

    def test_plain_run_never_touches_html(self):  # AC6
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=1)
            sentinel = root / "STATUS.html"
            sentinel.write_text("SENTINEL", encoding="utf-8")
            r = run_status(root)  # plain — no --html flag
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "SENTINEL",
                             "plain Stop-hook run must not write STATUS.html")


if __name__ == "__main__":
    unittest.main(verbosity=2)
