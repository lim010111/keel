#!/usr/bin/env python3
"""Regenerate STATUS.md for whatever project is the current working directory.

Global status harness. Invoked by the Stop hook in every project and by the
/status skill. It is project-agnostic and opt-in:

- The project root is the current git toplevel (falling back to cwd).
- It acts ONLY when the project has issue files at `.scratch/*/issues/*.md`
  (the local-markdown issue-tracker convention). Any other repo is a silent
  no-op, so the global Stop hook is harmless everywhere.

The mechanical sections (issue table, progress bar) are fully regenerated, and
a staleness banner is added when the narrative provably contradicts the issue
state. The narrative block between the two markers is preserved verbatim — that
is the only part a human or the /status skill edits. Drift is impossible by
construction: the table is always a projection of the issue files.

`--html` (status-harness#07) renders a second, human-only sibling projection,
`STATUS.html` — a card-per-track glance dashboard — from the SAME issue files,
so it cannot drift from STATUS.md. It is gitignored, generated on demand by
`/status --html` (regenerate → open), and never written on a plain Stop-hook
run. The cards are emitted as HTML directly from structured data; only the
narrative block is markdown-source, converted by a small stdlib-`re` converter
(no markdown dependency — ADR-0031 self-containment).

THE INVARIANT (do not "fix" this into a regression): each card's next-action is
COMPUTED FROM ISSUE STATE — `next_pointer()`: the in-progress issue, else the
lowest-numbered todo (a todo already implies its blockers are resolved; blocked
is skipped), else none (dormant/all-done) — with its title pulled from the issue
file. It is NOT parsed from the narrative's `Start here next session` prose. The
card pointer and the narrative Start-here may therefore legitimately diverge:
cross-cutting actions that are not issues (keel lockstep, "commit #01's output")
live only in the narrative. Parsing the narrative to "reconcile" them would
re-introduce the exact drift the status harness exists to kill.
"""
import os
import re
import subprocess
import sys
from html import escape
from pathlib import Path

NARRATIVE_START = "<!-- narrative:start -->"
NARRATIVE_END = "<!-- narrative:end -->"

# Soft ceiling for the narrative block, in characters (~1k tokens). A healthy
# narrative is ~1.5k chars; this is ~2.7x that, so the banner only fires on
# real drift from status-board into changelog — not on a normally-sized
# narrative (status-harness#03 lowered it from 6000 once the completion-label
# lint removed the main bloat source).
NARRATIVE_SOFT_LIMIT = 4000

# Track labels are a closed set ({능동, 병행, 휴면}): a finished track's line
# is DELETED from the narrative, never relabelled to a completion word — that
# is how the board regrows into a changelog (status-harness#03). This denylist
# anchors at the top-level bold bullet-label position only, so prose mentions,
# indented continuations, and Open-decisions issue-name labels never fire.
# Korean labels prefix-match (완료된/종료됨 fire too); English ones are
# word-bounded. Habitual relabelling is the observed failure mode — synonym
# invention is not pre-armoured against until observed.
COMPLETION_LABEL_RE = re.compile(
    r"^- \*\*\s*(?:완료|종료|완수|마감|하드닝-done"
    r"|(?:done|shipped|closed|finished|merged|landed)\b)",
    re.I)

DEFAULT_NARRATIVE = """\
## Current focus

_(What is being worked on right now — one or two sentences. Edit this, or run /status.)_

## Start here next session

- _(The concrete next action. Name the issue number.)_

## Open decisions

- _(Unresolved questions worth not forgetting. Empty is fine.)_
"""

EMOJI = {
    "done": "✅ done",
    "in-progress": "🔵 in-progress",
    "blocked": "⛔ blocked",
    "todo": "⬜ todo",
    "wontfix": "🚫 wontfix",
    "parked": "⏸️ parked",
    "?": "❔ unknown",
}


def project_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def parse_issue(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    m = re.match(r"\d+", path.stem)
    num = m.group(0) if m else "??"

    title = next((l[2:].strip() for l in lines if l.startswith("# ")), None)
    if title is None:
        # to-issues' issue template emits no H1; derive a readable title
        # from the "<NN>-<slug>" filename instead of showing the raw stem.
        slug = re.sub(r"^\d+[-_]", "", path.stem).replace("-", " ").replace("_", " ")
        title = slug.strip().capitalize() or path.stem

    sm = re.search(r"^Status:\s*(.+)$", text, re.M)
    triage = sm.group(1).strip() if sm else "—"

    # Count acceptance-criteria checkboxes.
    done = total = 0
    section = None
    for l in lines:
        if l.startswith("## "):
            section = l[3:].strip().lower()
            continue
        if section == "acceptance criteria":
            if re.match(r"\s*- \[[xX]\]", l):
                done += 1
                total += 1
            elif re.match(r"\s*- \[ \]", l):
                total += 1

    # Blockers — only bullet lines under "## Blocked by" count, in any of the
    # forms "- Issue 03 (...)", "- #03", or "- 03-slug.md". Prose mentions of
    # issue numbers ("independent of issues 02-05") are not bullets, so are
    # ignored — the leading "- " anchor is what keeps prose from false-firing.
    bm = re.search(r"^## Blocked by\s*\n(.*?)(?=\n## |\Z)", text, re.S | re.M)
    blockers = []
    if bm:
        blockers = sorted(set(re.findall(
            r"^\s*-\s+(?:[Ii]ssues?\s+)?#?\s*0*(\d+)\b", bm.group(1), re.M)))
        blockers = [b.zfill(2) for b in blockers]

    return {"num": num, "title": title, "triage": triage,
            "done": done, "total": total, "blockers": blockers}


def lifecycle(issue: dict, by_num: dict, _path: frozenset = frozenset()) -> str:
    # A wontfix issue is dead by triage decision, regardless of its checkboxes.
    if issue["triage"] == "wontfix":
        return "wontfix"
    # A parked issue is deferred until operator opt-in (ADR-0009) — not decided
    # against, but excluded from active progress the same way and surfaced as a
    # visible tombstone row rather than hidden.
    if issue["triage"] == "parked":
        return "parked"
    if issue["total"] == 0:
        return "?"
    if issue["done"] == issue["total"]:
        return "done"
    if issue["done"] > 0:
        return "in-progress"
    if issue["num"] in _path:
        return "todo"  # blocker cycle — break it rather than recurse forever
    _path = _path | {issue["num"]}
    for b in issue["blockers"]:
        blk = by_num.get(b)
        if blk and lifecycle(blk, by_num, _path) != "done":
            return "blocked"
    return "todo"


def bar(frac: float, width: int = 22) -> str:
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)


def read_narrative(status: Path) -> str:
    if not status.exists():
        return DEFAULT_NARRATIVE
    text = status.read_text(encoding="utf-8")
    m = re.search(re.escape(NARRATIVE_START) + r"\n(.*?)\n" + re.escape(NARRATIVE_END),
                  text, re.S)
    return m.group(1) if m else DEFAULT_NARRATIVE


def stale_warning(narrative: str, state_by_num: dict) -> str:
    """Return a one-line staleness banner for the narrative, or '' if current.

    Only two falsifiable signals fire it (see the status-harness grill, Q5);
    a mere issue-state change never does, because that is not proof the
    narrative is wrong and false positives train the reader to ignore it:

      (a) the narrative is still the unedited template;
      (b) every issue '## Start here next session' names is done, wontfix, or
          missing, so the section points at no live work.

    Prose staleness with no issue reference is left to a human or the /status
    skill by design — the narrative is the authored half of the document.
    """
    if narrative.strip() == DEFAULT_NARRATIVE.strip():
        return ("> ⚠️ **Narrative not written yet** — the block below is still "
                "the template. Run `/status` to fill it in.")

    m = re.search(r"^## Start here next session\s*\n(.*?)(?=\n## |\Z)",
                  narrative, re.S | re.M)
    if not m:
        return ""
    nums = [n.zfill(2) for n in re.findall(r"(?:issue\s+|#)\s*0*(\d+)",
                                           m.group(1), re.I)]
    if not nums:
        return ""
    if any(state_by_num.get(n) not in (None, "done", "wontfix") for n in nums):
        return ""  # at least one referenced issue is still live
    refs = ", ".join(f"#{n}" for n in sorted(set(nums)))
    return ("> ⚠️ **Narrative may be stale** — \"Start here next session\" "
            f"points to {refs}, now done or no longer present. Run `/status`.")


def size_warning(narrative: str) -> str:
    """Return a one-line banner when the narrative has bloated past the soft
    limit, or '' if within budget.

    The narrative is preserved verbatim — it is the authored half of the
    document — so nothing trims it automatically. This banner is the only
    mechanical signal that it has drifted from status-board into changelog.
    Like stale_warning it is *falsifiable* (literally over the limit), so it
    will not train the reader to ignore it. The fix is always the same: move
    per-session forensics (commits, finding-IDs, history) into the issue
    Resolution blocks and keep Current focus to posture + pointers.
    """
    n = len(narrative)
    if n <= NARRATIVE_SOFT_LIMIT:
        return ""
    return (f"> ⚠️ **Narrative oversized** ({n:,} chars > {NARRATIVE_SOFT_LIMIT:,}) "
            "— STATUS.md is a status board, not a changelog. Move per-session "
            "detail (commits, finding-IDs, history) into the issue `Resolution` "
            "blocks; keep *Current focus* to posture + pointers. See "
            "docs/agents/issue-tracker.md § STATUS.md editing rules.")


def completion_offenders(narrative: str) -> list[str]:
    """Top-level narrative bullets whose bold label opens with a completion
    word. Single source for both the advisory banner here and the blocking
    lint in narrative_guard.py (which imports this module)."""
    return [l.rstrip() for l in narrative.splitlines()
            if COMPLETION_LABEL_RE.match(l)]


def completion_warning(narrative: str) -> str:
    """Return a one-line banner when the narrative carries completed-track
    lines, or '' if clean.

    Advisory only — the blocking half lives in narrative_guard.py and fires
    only on lines written this session. This banner is the standing signal
    that covers what the guard cannot: pre-existing offenders and edits made
    outside a Claude Code Stop cycle (cross-agent, manual)."""
    n = len(completion_offenders(narrative))
    if not n:
        return ""
    return (f"> ⚠️ **Completed-track lines in narrative** ({n}) — a finished "
            "track's line is deleted, not relabelled (labels are 능동/병행/휴면 "
            "only). Detail → the issue's `Resolution` block; posture outcome → "
            "the gate-table cell. See docs/agents/issue-tracker.md § The "
            "narrative is a status board, not a changelog.")


def feature_section(feature: str, files: list[Path]) -> tuple[str, int, int, dict]:
    """Render one feature's progress bar + issue table.

    Returns (markdown, criteria_done, criteria_total, state_by_num, issues).
    Blocker resolution is scoped to this feature, so issue numbers never
    collide across features.
    """
    issues = [parse_issue(p) for p in sorted(files)]
    by_num = {i["num"]: i for i in issues}

    # wontfix issues stay in the table but are dead work; parked issues stay
    # but are deferred (ADR-0009 opt-in). Exclude both from the progress bar so
    # neither deflates the percentage.
    counted = [i for i in issues if i["triage"] not in ("wontfix", "parked")]
    done = sum(i["done"] for i in counted)
    total = sum(i["total"] for i in counted)
    frac = done / (total or 1)

    rows = []
    states = {}
    for i in issues:
        state = lifecycle(i, by_num)
        states[i["num"]] = state
        blk = ", ".join(f"#{b}" for b in i["blockers"]) or "—"
        rows.append(f"| {i['num']} | {i['title']} | `{i['triage']}` | "
                    f"{i['done']}/{i['total']} | {EMOJI[state]} | {blk} |")

    md = f"""## {feature}

`{bar(frac)}` {done}/{total} acceptance criteria met ({frac:.0%})

| # | Issue | Triage | Criteria | State | Blocked by |
|---|-------|--------|----------|-------|-----------|
{chr(10).join(rows)}"""
    return md, done, total, states, issues


# Lifecycle states worth a row in the brief session-start injection — the work
# a session could actually pick up (or unblock). done/parked/wontfix rows are
# orientation the narrative + progress fraction already carry; injecting them
# every session was the dominant standing context tax (status-harness#03).
ACTIONABLE_STATES = ("todo", "in-progress", "blocked", "?")


def feature_brief(feature: str, issues: list[dict], states: dict,
                  done: int, total: int) -> str:
    """One feature's brief block: progress line + actionable rows only.
    Everything else collapses to a counted omission line — the full table
    lives in STATUS.md (file ≠ injection)."""
    frac = done / (total or 1)
    head = f"## {feature} — `{bar(frac)}` {done}/{total} ({frac:.0%})"

    live = [i for i in issues if states[i["num"]] in ACTIONABLE_STATES]
    omitted = []
    for kind in ("done", "parked", "wontfix"):
        n = sum(1 for i in issues if states[i["num"]] == kind)
        if n:
            omitted.append(f"{n} {kind}")
    note = ("… " + " + ".join(omitted) + " omitted — full table: STATUS.md"
            if omitted else "")

    if not live:
        return head + (f"\n{note}" if note else "")
    rows = [f"| {i['num']} | {i['title']} | {i['done']}/{i['total']} | "
            f"{EMOJI[states[i['num']]]} |" for i in live]
    table = ("| # | Issue | Criteria | State |\n"
             "|---|-------|----------|-------|\n" + "\n".join(rows))
    return "\n".join(p for p in (head, "", table, note) if p != "")


# ==========================================================================
# STATUS.html — human glance dashboard (status-harness#07). A sibling
# projection of the SAME issue files; see the module docstring for the
# computed-pointer invariant (AC3/AC7).
# ==========================================================================

# Order in which lifecycle states are surfaced as count-chips on a card.
STATE_ORDER = ("in-progress", "todo", "blocked", "done", "parked", "wontfix", "?")
STATE_LABEL = {"in-progress": "in-progress", "todo": "todo", "blocked": "blocked",
               "done": "done", "parked": "parked", "wontfix": "wontfix",
               "?": "unknown"}


def _num_key(issue: dict) -> float:
    try:
        return int(issue["num"])
    except (ValueError, TypeError):
        return float("inf")


def next_pointer(issues: list[dict], states: dict) -> dict | None:
    """The card's next-action, computed PURELY from issue lifecycle state.

    in-progress (lowest #) → else lowest-numbered todo → else None. blocked,
    done, parked and wontfix are never pointed at. Reads ONLY issue state —
    never the narrative (the invariant; see module docstring, AC3/AC7)."""
    in_progress = [i for i in issues if states[i["num"]] == "in-progress"]
    if in_progress:
        return min(in_progress, key=_num_key)
    todos = [i for i in issues if states[i["num"]] == "todo"]
    if todos:
        return min(todos, key=_num_key)
    return None


def _inline(text: str) -> str:
    """Inline markdown → HTML on already-escaped text: code, bold, links."""
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def md_to_html(md: str) -> str:
    """A tiny stdlib-only markdown→HTML converter for the narrative block.

    Handles the subset the narrative actually uses — HTML-comment stripping,
    ATX headings, bold/inline-code/links, bullet lists (with indented
    continuation lines), pipe tables (the gate table), and paragraphs. Exotic
    constructs may mis-render; that is acceptable. A new import is not
    (ADR-0031). All text is HTML-escaped before inline markup is applied."""
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)  # comments never render
    lines = md.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", line)
        if m:  # heading
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(escape(m.group(2).strip()))}</h{lvl}>")
            i += 1
        elif (line.lstrip().startswith("|") and i + 1 < n
              and re.match(r"\s*\|[\s:|-]+\|?\s*$", lines[i + 1])):  # table
            head = _cells(line)
            i += 2
            body = []
            while i < n and lines[i].lstrip().startswith("|"):
                body.append(_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{_inline(escape(c))}</th>" for c in head)
            rows = "".join("<tr>" + "".join(f"<td>{_inline(escape(c))}</td>"
                           for c in r) + "</tr>" for r in body)
            out.append(f"<table><thead><tr>{th}</tr></thead>"
                       f"<tbody>{rows}</tbody></table>")
        elif re.match(r"\s*-\s+", line):  # bullet list w/ indented continuations
            items: list[str] = []
            while i < n and (re.match(r"\s*-\s+", lines[i])
                             or (lines[i][:1] == " " and lines[i].strip() and items)):
                if re.match(r"\s*-\s+", lines[i]):
                    items.append(re.sub(r"\s*-\s+", "", lines[i], count=1))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            lis = "".join(f"<li>{_inline(escape(it.strip()))}</li>" for it in items)
            out.append(f"<ul>{lis}</ul>")
        else:  # paragraph
            para = [line]
            i += 1
            while (i < n and lines[i].strip()
                   and not re.match(r"(#{1,6})\s|\s*-\s+|\s*\|", lines[i])):
                para.append(lines[i])
                i += 1
            out.append(f"<p>{_inline(escape(' '.join(s.strip() for s in para)))}</p>")
    return "\n".join(out)


def _issue_table(issues: list[dict], states: dict) -> str:
    rows = []
    for i in issues:
        blk = ", ".join(f"#{b}" for b in i["blockers"]) or "—"
        rows.append(
            f'<tr><td>{escape(i["num"])}</td>'
            f'<td>{_inline(escape(i["title"]))}</td>'
            f'<td><code>{escape(i["triage"])}</code></td>'
            f'<td>{i["done"]}/{i["total"]}</td>'
            f'<td>{escape(EMOJI[states[i["num"]]])}</td>'
            f'<td>{escape(blk)}</td></tr>')
    return ('<table class="issues"><thead><tr><th>#</th><th>Issue</th>'
            '<th>Triage</th><th>Criteria</th><th>State</th><th>Blocked by</th>'
            '</tr></thead><tbody>' + "".join(rows) + "</tbody></table>")


def _card(feature: str, done: int, total: int, states: dict,
          issues: list[dict]) -> str:
    frac = done / (total or 1)
    pct = f"{frac:.0%}"
    counts = {s: sum(1 for i in issues if states[i["num"]] == s) for s in STATE_ORDER}
    chips = "".join(f'<span class="chip s-{s}">{counts[s]} {STATE_LABEL[s]}</span>'
                    for s in STATE_ORDER if counts[s])
    nxt = next_pointer(issues, states)
    if nxt:
        nxt_html = (f'<div class="next"><span class="next-num">#{escape(nxt["num"])}'
                    f'</span>{_inline(escape(nxt["title"]))}</div>')
    elif total and done == total:
        nxt_html = '<div class="next idle">✓ 전부 완료</div>'
    else:
        nxt_html = '<div class="next idle">휴면 · 열린 작업 없음</div>'
    fill = "fill full" if frac >= 1 else "fill"
    return (f'<section class="card"><h2>{escape(feature)}'
            f'<span class="pct">{pct}</span></h2>'
            f'<div class="track"><div class="{fill}" style="width:{pct}"></div></div>'
            f'<div class="meta">{done}/{total} criteria</div>'
            f'<div class="chips">{chips}</div>{nxt_html}'
            f'<details><summary>{len(issues)} issues</summary>'
            f'{_issue_table(issues, states)}</details></section>')


CSS = """\
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--muted:#6b7280;--line:#e5e7eb;
--accent:#2563eb;--green:#16a34a;--red:#dc2626}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans KR",sans-serif}
.top{max-width:1200px;margin:0 auto;padding:24px 28px 4px}
.top h1{margin:0 0 4px;font-size:22px}
.overall{color:var(--muted);font-size:13px;margin-bottom:8px}
.gen{color:var(--muted);font-size:12px;margin:8px 0 0}
.track{height:8px;background:var(--line);border-radius:99px;overflow:hidden}
.track.big{height:10px}
.track .fill{height:100%;background:var(--accent)}
.track .fill.full{background:var(--green)}
.banners{max-width:1200px;margin:12px auto 0;padding:0 28px}
.warn{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;
padding:8px 12px;border-radius:8px;margin:6px 0;font-size:13px}
.cards{max-width:1200px;margin:18px auto;padding:0 28px;display:grid;gap:16px;
grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card h2{margin:0 0 10px;font-size:16px;display:flex;
justify-content:space-between;align-items:baseline;gap:8px}
.card .pct{color:var(--muted);font-size:14px;font-weight:600}
.card .meta{color:var(--muted);font-size:12px;margin:8px 0 10px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.chip{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);
color:var(--muted)}
.chip.s-done{color:var(--green);border-color:#bbf7d0;background:#f0fdf4}
.chip.s-in-progress{color:var(--accent);border-color:#bfdbfe;background:#eff6ff}
.chip.s-blocked{color:var(--red);border-color:#fecaca;background:#fef2f2}
.next{font-size:14px;padding:10px 12px;border-radius:8px;background:#f0f6ff;
border:1px solid #dbeafe}
.next .next-num{font-weight:700;color:var(--accent);margin-right:6px}
.next.idle{background:#f9fafb;border-color:var(--line);color:var(--muted)}
details{margin-top:12px}
summary{cursor:pointer;font-size:13px;color:var(--muted)}
table.issues{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px}
table.issues th,table.issues td{text-align:left;padding:5px 6px;
border-bottom:1px solid var(--line);vertical-align:top}
table.issues th{color:var(--muted);font-weight:600}
.narrative{max-width:1200px;margin:24px auto 60px;padding:8px 28px 24px;
background:var(--card);border:1px solid var(--line);border-radius:12px}
.narrative h2{font-size:17px;margin:22px 0 8px;padding-bottom:4px;
border-bottom:1px solid var(--line)}
.narrative table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}
.narrative th,.narrative td{border:1px solid var(--line);padding:6px 8px;
text-align:left;vertical-align:top}
.narrative th{background:#f9fafb}
code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:.9em;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
a{color:var(--accent)}\
"""


def render_html(narrative: str, warnings: list[str],
                feature_data: list[tuple], total_done: int,
                total_crit: int) -> str:
    """Build the single self-contained STATUS.html document (inline CSS, no JS,
    no build step). `feature_data` is the per-feature
    (feature, done, total, states, issues) reused from main()'s loop."""
    cards = "\n".join(_card(*fd) for fd in feature_data)
    frac = total_done / (total_crit or 1)
    banner = ""
    if warnings:
        ws = "".join(f'<div class="warn">{_inline(escape(w.lstrip("> ").strip()))}'
                     f'</div>' for w in warnings)
        banner = f'<div class="banners">{ws}</div>'
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project Status</title>
<style>
{CSS}
</style>
</head>
<body>
<header class="top">
<h1>Project Status</h1>
<div class="overall">{total_done}/{total_crit} acceptance criteria · \
{len(feature_data)} tracks · {frac:.0%}</div>
<div class="track big"><div class="fill" style="width:{frac:.0%}"></div></div>
<p class="gen">Generated by the status harness (<code>status.py --html</code>) — \
human glance view. Source of truth is the issue files; never hand-edit.</p>
</header>
{banner}
<main class="cards">
{cards}
</main>
<section class="narrative">
{md_to_html(narrative)}
</section>
</body>
</html>
"""


def open_html(path: Path) -> None:
    """Best-effort open in the platform browser, with a printed-path fallback.

    Tries WSL openers first (this environment is WSL2), then the generic
    desktop openers. Never hard-fails the run. `STATUS_HTML_NO_OPEN` skips the
    launch and just prints the path — for headless / automated / test runs."""
    if os.environ.get("STATUS_HTML_NO_OPEN"):
        print(f"STATUS.html written — open it: {path}")
        return
    for cmd in (["wslview"], ["explorer.exe"], ["xdg-open"], ["open"]):
        try:
            subprocess.run(cmd + [str(path)], check=True, capture_output=True,
                           timeout=10)
            print(f"STATUS.html → opened ({path})")
            return
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            continue
    print(f"STATUS.html written — open it manually: {path}")


def main(argv: list[str] | None = None) -> None:
    # No-op inside a merge-gate produce subprocess: the validator child runs as a
    # fresh `claude -p` session that loads settings, so its Stop/SessionStart fire
    # here; regenerating STATUS.md per produce is spurious churn (#31 seed finding).
    if os.environ.get("MERGE_GATE_PRODUCER_RUNNING") == "1":
        return
    # --brief: regenerate the file exactly as a plain run would, but print the
    # brief session-start view instead of the regen message. The SessionStart
    # hook injects this stdout, so it must contain ONLY the view.
    args = sys.argv[1:] if argv is None else argv
    brief = "--brief" in args
    want_html = "--html" in args
    root = project_root()
    issue_files = sorted((root / ".scratch").glob("*/issues/*.md"))
    if not issue_files:
        return  # Opt-in: no local-markdown issues here — silent no-op.

    status = root / "STATUS.md"

    # Group issues by feature directory: .scratch/<feature>/issues/*.md
    by_feature: dict[str, list[Path]] = {}
    for p in issue_files:
        by_feature.setdefault(p.parent.parent.name, []).append(p)

    sections = []
    briefs = []
    feature_data: list[tuple] = []
    state_by_num: dict[str, str] = {}
    total_done = total_crit = 0
    for feature in sorted(by_feature):
        md, done, total, states, issues = feature_section(feature, by_feature[feature])
        sections.append(md)
        briefs.append(feature_brief(feature, issues, states, done, total))
        feature_data.append((feature, done, total, states, issues))
        total_done += done
        total_crit += total
        # Cross-feature number collisions are rare and narrative refs are
        # feature-agnostic; last write wins.
        state_by_num.update(states)

    narrative = read_narrative(status)
    warnings = [w for w in (stale_warning(narrative, state_by_num),
                            size_warning(narrative),
                            completion_warning(narrative)) if w]
    banner = ("\n\n" + "\n".join(warnings)) if warnings else ""

    content = f"""# Project Status

_Generated by the status harness (`~/.claude/scripts/status.py`) — do not hand-edit
outside the narrative block; mechanical sections are regenerated every run._{banner}

{NARRATIVE_START}
{narrative}
{NARRATIVE_END}

{(chr(10) + chr(10)).join(sections)}

State is derived: all criteria checked → `done`; some → `in-progress`; none
with an unfinished blocker → `blocked`; otherwise → `todo`. Issues triaged
`wontfix` (decided against) or `parked` (deferred until operator opt-in) show
that triage state and are excluded from the progress bar.
"""
    # Only write when something changed, so the Stop hook does not produce a
    # no-op diff every session. Generated content is now date-free, so an
    # unchanged project yields byte-identical output run after run.
    old = status.read_text(encoding="utf-8") if status.exists() else ""
    if content != old:
        status.write_text(content, encoding="utf-8")
    # --html: also emit the human glance sibling and open it. Gated entirely
    # behind the flag, so a plain Stop-hook run never writes/opens STATUS.html.
    if want_html:
        html_path = root / "STATUS.html"
        html_path.write_text(
            render_html(narrative, warnings, feature_data, total_done, total_crit),
            encoding="utf-8")
        open_html(html_path)
        return
    if brief:
        parts = ["\n".join(warnings)] if warnings else []
        parts.append(narrative)
        parts.extend(briefs)
        print("\n\n".join(parts))
    elif content == old:
        print(f"STATUS.md unchanged — {total_done}/{total_crit} criteria.")
    else:
        print(f"STATUS.md regenerated — {total_done}/{total_crit} criteria, "
              f"{len(issue_files)} issues, {len(by_feature)} feature(s).")


if __name__ == "__main__":
    main()
