#!/usr/bin/env python3
"""Tests for status.py's completion-label lint + --brief view
(claude-harness-work status-harness#03).

Stdlib unittest only — pytest is not installed in this environment.
Run:  python3 scripts/test_status.py -v
"""
from __future__ import annotations

import inspect
import json
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
               done: int = 0, status_line: str = "ready-for-agent",
               title: str | None = None) -> Path:
    d = root / ".scratch" / feature / "issues"
    d.mkdir(parents=True, exist_ok=True)
    h1 = title if title is not None else slug.replace('-', ' ').title()
    lines = [f"# {h1}", "",
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
# _json_payload — Layer-2 <script>-safety: the embedded projectData literal
# must never let data break out of the inline <script> block.
# --------------------------------------------------------------------------
class TestJsonPayload(unittest.TestCase):
    def test_script_close_and_html_meta_are_unicode_escaped(self):
        payload = status._json_payload({"x": "</script><b>tom & jerry"})
        # No literal HTML metacharacters survive in the serialized text, so the
        # parser scanning the raw <script> body can never see </script>.
        self.assertNotIn("</script>", payload)
        self.assertNotIn("<b>", payload)
        self.assertIn("\\u003c", payload)
        self.assertIn("\\u003e", payload)
        self.assertIn("\\u0026", payload)
        # Value-preserving: the JS engine decodes \uXXXX back to the original.
        self.assertEqual(json.loads(payload)["x"], "</script><b>tom & jerry")

    def test_korean_is_kept_readable(self):
        # ensure_ascii=False keeps Hangul as itself (UTF-8 page), not \uXXXX.
        self.assertIn("능동", status._json_payload({"k": "능동"}))


# --------------------------------------------------------------------------
# --html end-to-end — AC1/AC2/AC3/AC6 + self-containment + escaping.
# The dashboard renders client-side from an embedded projectData literal, so
# the structural assertions parse that literal rather than scraping the DOM.
# --------------------------------------------------------------------------
def extract_project_data(html: str) -> dict:
    """Parse the embedded `const projectData = {...};` single-line literal."""
    prefix = "const projectData = "
    line = next((l for l in html.splitlines() if l.startswith(prefix)), None)
    assert line is not None, "projectData literal not found in STATUS.html"
    payload = line[len(prefix):].rstrip()
    assert payload.endswith(";"), repr(payload[-40:])
    return json.loads(payload[:-1])


class TestHtmlEndToEnd(unittest.TestCase):
    def _run_html(self, root: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "STATUS_HTML_NO_OPEN": "1"}
        return subprocess.run(["python3", str(STATUS_PY), "--html"], cwd=str(root),
                              capture_output=True, text=True, timeout=60, env=env)

    def _html(self, root: Path) -> str:
        r = self._run_html(root)
        self.assertEqual(r.returncode, 0, r.stderr)
        return (root / "STATUS.html").read_text(encoding="utf-8")

    def test_no_op_without_issue_files(self):  # AC1
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            r = self._run_html(root)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((root / "STATUS.html").exists(),
                             "no issues → silent no-op, no HTML written")

    def test_well_formed_and_track_per_feature(self):  # AC1/AC2
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=1)
            seed_issue(root, "gamma", "01", "solo", total=2, done=2)
            text = self._html(root)
            self.assertTrue(text.lstrip().startswith("<!DOCTYPE html>"))
            self.assertTrue(text.rstrip().endswith("</html>"))
            self.assertEqual(text.count("<script>"), 1)
            self.assertEqual(text.count("</script>"), 1)
            data = extract_project_data(text)
            self.assertEqual([t["name"] for t in data["tracks"]], ["feat", "gamma"])
            self.assertEqual(data["summary"]["tracksCount"], 2)
            self.assertIn("percent", data["summary"])

    def test_next_pointer_is_computed_from_state(self):  # AC2/AC3
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            # active track: lowest-numbered todo (#02) is the pointer
            seed_issue(root, "feat", "05", "echo-todo", total=2, done=0)
            seed_issue(root, "feat", "02", "bravo-todo", total=2, done=0)
            # all-done track → ✓; dormant (wontfix-only) track → 휴면
            seed_issue(root, "donezo", "01", "alpha", total=2, done=2)
            seed_issue(root, "sleepy", "01", "dead", total=2, done=0,
                       status_line="wontfix")
            data = extract_project_data(self._html(root))
            tracks = {t["name"]: t for t in data["tracks"]}
            self.assertEqual(tracks["feat"]["nextNum"], "#02")
            self.assertIn("Bravo Todo", tracks["feat"]["next"])
            self.assertEqual(tracks["feat"]["status"], "in-progress")
            self.assertEqual(tracks["donezo"]["nextNum"], "✓")
            self.assertEqual(tracks["donezo"]["status"], "settled")
            self.assertEqual(tracks["sleepy"]["nextNum"], "휴면")

    def test_no_external_resource(self):  # ADR-0031 self-containment
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=1)
            text = self._html(root)
            for bad in ("@import", "fonts.googleapis", "fonts.gstatic",
                        "<link ", 'src="http', "src='http"):
                self.assertNotIn(bad, text, f"external dep leaked: {bad}")

    def test_dangerous_issue_title_is_escaped(self):  # escaping (Layer 1)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "danger", total=2, done=0,
                       title='<script>alert(1)</script> & "x"')
            text = self._html(root)
            self.assertEqual(text.count("</script>"), 1,
                             "a hostile title must not inject a 2nd </script>")
            data = extract_project_data(text)
            issue = data["tracks"][0]["issues"][0]
            self.assertIn("&lt;script&gt;", issue["title"])
            self.assertNotIn("<script>", issue["title"])
            self.assertIn("&amp;", issue["title"])

    def test_hostile_next_pointer_title_is_render_safe(self):  # dual-sink resolution
        # track.next is a DUAL-SINK field: the drawer reads it via
        # drawerNextAction.innerText (entities rendered literally → must store
        # RAW), and the card reads it via renderTracks `${track.next}` innerHTML.
        # The resolution is to store the RAW title and escape at the card render
        # site with esc(track.next); the <script> embedding is protected
        # independently by _json_payload's unicode-escape (Layer 2). So a hostile
        # title lives RAW in projectData, never breaks the <script>, and is
        # escaped where it reaches innerHTML.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            # done=0/total=2, no in-progress → #01 is the computed next pointer.
            seed_issue(root, "feat", "01", "danger", total=2, done=0,
                       title='<img src=x onerror=alert(1)>')
            text = self._html(root)
            track = extract_project_data(text)["tracks"][0]
            self.assertIn("<img", track["next"],
                          "track.next is stored RAW for the innerText drawer sink")
            self.assertEqual(text.count("</script>"), 1,
                             "Layer-2 unicode-escape protects the <script> embedding")
            self.assertIn("esc(track.next)", text,
                          "the card render site escapes the innerHTML sink")

    def test_next_pointer_raw_for_innertext_sink(self):  # claude:finding-0
        # Finding 1 (dual-sink innerText double-encoding). The drawer renders
        # the next-pointer via `drawerNextAction.innerText = track.next`
        # (status.py ~2041). innerText renders HTML entities LITERALLY, so the
        # value in projectData must be the RAW title text. HEAD escapes it at
        # the source (escape(_strip_md(...)) at ~594), so a BENIGN title with
        # '&' is stored as '&amp;' and the drawer shows the literal "&amp;".
        # Correct invariant: track.next must be RAW (card-innerHTML safety
        # belongs at render time, not baked into the stored value).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            # done=0/total=2, no in-progress → #01 is the computed next pointer.
            seed_issue(root, "feat", "01", "amp", total=2, done=0,
                       title="auth & session")
            track = extract_project_data(self._html(root))["tracks"][0]
            self.assertIn("auth & session", track["next"],
                          "track.next feeds drawerNextAction.innerText — must be "
                          "the raw title so the drawer renders it correctly")
            self.assertNotIn("&amp;", track["next"],
                             "double-encoded '&amp;' shows as a literal entity "
                             "in the innerText drawer sink")

    def test_track_button_not_inline_onclick(self):  # codex:finding-0
        # Finding 2 (inline onclick attribute-context XSS via track.name).
        # track.name is a .scratch/<feature>/ dir slug (attacker-controllable in
        # a crafted repo). HEAD interpolates it into an inline event-handler
        # attribute: onclick="openTrackDetails('${track.name}')" (status.py
        # ~2022). html.escape turns ' into &#x27;, but the HTML parser DECODES
        # that back to ' inside the attribute before the JS string is evaluated,
        # so a dir name like x');alert(1);(' breaks out and executes. Root-cause
        # invariant: a free-form track field must NOT be interpolated into an
        # inline event handler (the fix is a data-* attribute + addEventListener).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "x');alert(1);('", "01", "alpha", total=2, done=0)
            text = self._html(root)
            self.assertNotIn("onclick=\"openTrackDetails('", text,
                             "track.name must not be interpolated into an inline "
                             "onclick handler — html.escape does not survive the "
                             "HTML-attribute decode (use data-* + addEventListener)")

    def test_hostile_issue_status_is_escaped(self):  # codex:finding-0
        # Finding (raw issue status → drawer innerHTML XSS). The drawer issues
        # table writes issue.triage straight into tbody.innerHTML (status.py
        # ~2047: <span class="issue-triage">${issue.triage}</span>) with NO
        # esc() at the sink. triage is derived from the free-form `Status:`
        # line (parse_issue ~119, no label-set validation) and stored RAW in
        # projectData (_track_data ~600: "triage": i["triage"]). Unlike the
        # dual-sink track.next/track.name (which also feed innerText), issue
        # .triage is an innerHTML-only single-sink plain-text field, so the fix
        # is escape-at-SOURCE — parallel to the sibling issue.title (~599:
        # _inline(escape(i["title"]))). _json_payload's unicode-escape only
        # guards the <script> embedding; the browser decodes the value back
        # before the innerHTML assignment, so an <img onerror> still fires.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=0,
                       status_line='<img src=x onerror=alert(1)>')
            text = self._html(root)
            issue = extract_project_data(text)["tracks"][0]["issues"][0]
            self.assertNotIn("<img", issue["triage"],
                             "issue.triage reaches tbody.innerHTML — a raw "
                             "<img onerror> executes when the drawer opens")
            self.assertIn("&lt;img", issue["triage"],
                          "hostile Status: line must be escaped at the source "
                          "(parallel to the already-escaped issue.title)")
            self.assertEqual(text.count("</script>"), 1,
                             "Layer-2 unicode-escape keeps the <script> intact")

    def test_no_free_form_field_reaches_innerhtml_unescaped(self):  # claude:finding-0
        # Class-guard for the "split escaping model" concern: source-escaped vs
        # render-escaped, with no guard, a future missed escape silently
        # reintroduces XSS. Seed HOSTILE values in EVERY free-form parse position
        # and assert each is neutralized at its sink — every free-form innerHTML
        # sink is either source-escaped (no raw markup in projectData) or
        # render-escaped (esc() wrapped in the template). This FAILS on HEAD
        # (issue.triage is raw) and passes once triage is escaped at the source.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "<img src=x onerror=alert(1)>", "01", "alpha",
                       total=2, done=0,
                       status_line="<img src=z onerror=alert(3)>",
                       title="<img src=y onerror=alert(2)>")
            text = self._html(root)
            track = extract_project_data(text)["tracks"][0]
            issue = track["issues"][0]
            # innerHTML-only fields are escaped at the SOURCE → no raw markup.
            self.assertNotIn("<img", issue["title"])
            self.assertNotIn("<img", issue["triage"])
            # dual-sink fields are stored RAW (for the innerText drawer sink) and
            # escaped at the CARD RENDER SITE in the template.
            self.assertIn("esc(track.name)", text)
            self.assertIn("esc(track.next)", text)
            # Layer 2: the <script> embedding stays intact.
            self.assertEqual(text.count("</script>"), 1)

    def test_hostile_start_here_type_is_escaped(self):  # claude:finding-0
        # Finding (raw Start-here `item.type` → sidebar innerHTML XSS). The
        # narrative populator writes item.type straight into startHereList
        # .innerHTML (status.py ~1898: <div class="todo-badge">${item.type}
        # </div>) with NO esc() at the sink. type is the bold label BEFORE the
        # first `·` in a `## Start here next session` bullet, stored RAW in
        # projectData (_parse_start_here ~549: "type": parts[0] ...) while its
        # siblings track/text ARE escaped (~550/551). Unlike the dual-sink
        # track.next/track.name, item.type is an innerHTML-only single-sink
        # plain-text field, so the fix is escape-at-SOURCE — parallel to the
        # sibling track field and to issue.triage. getTodoItemClass(item.type)
        # on ~1897 only governs the CSS class, not the innerHTML text on ~1898.
        # The narrative is repo-controlled markdown (STATUS.md's authored
        # Start-here section), so a hostile bullet is in scope (status-harness#07).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=0)
            # Seed the authored narrative the way production sources it: an
            # existing STATUS.md whose narrative block carries a Start-here
            # bullet whose bold label (before the `·`) is hostile.
            hostile = "<img src=x onerror=alert(1)>"
            narrative = (
                "## Current focus\n\n"
                "prose.\n\n"
                "## Start here next session\n\n"
                f"- **{hostile} · prompt-improver:** #01 do the thing\n\n"
                "## Open decisions\n\n"
                "- none\n"
            )
            (root / "STATUS.md").write_text(
                f"{status.NARRATIVE_START}\n{narrative}\n{status.NARRATIVE_END}\n",
                encoding="utf-8")
            text = self._html(root)
            sh = extract_project_data(text)["narrative"]["startHere"]
            item = next(i for i in sh if i["track"] == "prompt-improver")
            self.assertNotIn("<img", item["type"],
                             "item.type reaches startHereList.innerHTML — a raw "
                             "<img onerror> executes when the sidebar renders")
            self.assertIn("&lt;img", item["type"],
                          "hostile Start-here label must be escaped at the source "
                          "(parallel to the already-escaped sibling track field)")
            self.assertEqual(text.count("</script>"), 1,
                             "Layer-2 unicode-escape keeps the <script> intact")

    def test_status_html_no_open_prints_path(self):  # AC6 opener fallback
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=1)
            r = self._run_html(root)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("STATUS.html", r.stdout)

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


# --------------------------------------------------------------------------
# Narrative decomposition — the sidebar slots are display projections of the
# narrative string; none of this feeds the per-track pointer (AC3 stays).
# --------------------------------------------------------------------------
class TestNarrativeData(unittest.TestCase):
    NARR = (
        "## Current focus\n\n"
        "Posture **bold** and `code` prose.\n\n"
        "| Gate | 상태 | Pointer |\n"
        "|---|---|---|\n"
        "| **alignment** | shipped · kind = x | `/grill-me` |\n"
        "| **merge** | local-only · advisory | ADR-0009 |\n"
        "| **distribution** | deferred | ADR-0006 |\n\n"
        "## Start here next session\n\n"
        "intro line, not a bullet\n\n"
        "- **능동 · prompt-improver:** #02 do the thing `now`\n"
        "- **휴면 · work-interval-tdd:** parked\n\n"
        "## Open decisions\n\n"
        "- **a backlog** — decide later\n")

    def test_focus_text_and_gates(self):
        d = status._narrative_data(self.NARR, [])
        self.assertIn("<strong>bold</strong>", d["currentFocus"]["text"])
        self.assertIn("<code>code</code>", d["currentFocus"]["text"])
        gates = d["currentFocus"]["gates"]
        self.assertEqual([g["name"] for g in gates],
                         ["alignment", "merge", "distribution"])
        self.assertEqual(gates[0]["stateClass"], "shipped")
        self.assertEqual(gates[1]["stateClass"], "active")
        self.assertEqual(gates[2]["stateClass"], "deferred")

    def test_start_here_and_open_decisions(self):
        d = status._narrative_data(self.NARR, [])
        sh = d["startHere"]
        self.assertEqual(len(sh), 2)
        self.assertEqual(sh[0]["type"], "능동")
        self.assertEqual(sh[0]["track"], "prompt-improver")
        self.assertIn("<code>now</code>", sh[0]["text"])
        self.assertEqual(len(d["openDecisions"]), 1)
        self.assertIn("<strong>a backlog</strong>", d["openDecisions"][0])

    def test_warnings_surface_in_focus(self):
        d = status._narrative_data(self.NARR,
                                   ["> ⚠️ **Narrative may be stale** — x"])
        self.assertIn("Narrative may be stale", d["currentFocus"]["text"])


# --------------------------------------------------------------------------
# Discoverability nudge venue (this session's /grill-with-docs). The
# "STATUS.html glance view exists" hint must live in the /status SKILL.md, so
# it fires ONLY on an explicit /status — never in status.py stdout, which the
# Stop hook runs every turn (a stdout hint would spam every Stop and defeat the
# very discoverability it is meant to add).
# --------------------------------------------------------------------------
STATUS_SKILL_MD = SCRIPTS.parent / "skills" / "status" / "SKILL.md"


class TestHtmlHintVenue(unittest.TestCase):
    def test_skill_md_surfaces_html_view_at_status_completion(self):
        """The /status skill nudges the user about the STATUS.html glance view
        at completion — anchored by the stable `html-hint` marker."""
        text = STATUS_SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("html-hint", text,
                      "/status SKILL.md must carry the completion-time STATUS.html "
                      "discoverability nudge (anchor marker `html-hint` missing)")
        self.assertIn("--html", text)

    def test_status_py_stdout_does_not_advertise_html(self):
        """Spam-prevention lock: the nudge must never leak into status.py
        stdout. The Stop hook runs a plain `status.py` every turn, so a stdout
        advert would fire on every Stop — the exact failure the SKILL.md venue
        was chosen to avoid."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            git_init(root)
            seed_issue(root, "alpha", "01", "thing", total=2, done=1)
            r = run_status(root)  # plain run — exactly what the Stop hook executes
            self.assertNotIn("--html", r.stdout,
                             "status.py stdout must not advertise --html (Stop-hook spam)")
            self.assertNotIn("STATUS.html", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
