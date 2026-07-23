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
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from html import escape
from pathlib import Path

# Journal (harness-journal#01), as "status". Strictly additive: the import is
# guarded (the absolute path also serves vendored copies in target repos; on a
# host without ~/.claude — e.g. GHA regen — it fails open to no-ops) and both
# callables never raise. status.py reads no stdin payload (it is also invoked
# interactively), so events carry no session_id; repo is passed explicitly.
try:
    sys.path.append(str(Path.home() / ".claude" / "scripts"))
    from hook_journal import for_hook as _for_hook
    _journal, _drift = _for_hook("status")
except Exception:
    _journal = _drift = lambda *a, **k: None

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


def _strip_md(text: str) -> str:
    """Plain text for a DUAL-SINK field (read by both innerHTML and innerText):
    drop markdown markers and leave it UN-escaped — innerText renders entities
    literally, so no single escaping satisfies both sinks. Safe only because the
    value-space (issue titles, sentinels) is metacharacter-light and the harness
    controls issue titles. See status-harness#07 escaping notes."""
    return re.sub(r"[`*]", "", text)


def _gate_state_class(cell: str) -> str:
    """Colour class for a gate-pipeline node — a small DISPLAY vocabulary
    ({shipped, active, wontfix, deferred}), classified from the gate-table prose
    cell, strongest signal first. Distinct from issue lifecycle; an unmatched
    cell degrades to the neutral "active". Display-only — never feeds the
    per-track pointer (AC3)."""
    s = cell.lower()
    if "폐기" in cell or "미인스턴스화" in cell or "retired" in s or "wontfix" in s:
        return "wontfix"
    if "shipped" in s:
        return "shipped"
    if "deferred" in s:
        return "deferred"
    return "active"


def _logical_bullets(section: str) -> list[str]:
    """Top-level `- ` bullets of a narrative section, each folding in its
    indented continuation lines (the same merge rule md_to_html uses)."""
    lines = section.splitlines()
    items: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if re.match(r"\s*-\s+", lines[i]):
            buf = re.sub(r"\s*-\s+", "", lines[i], count=1)
            i += 1
            while (i < n and lines[i][:1] == " " and lines[i].strip()
                   and not re.match(r"\s*-\s+", lines[i])):
                buf += " " + lines[i].strip()
                i += 1
            items.append(buf.strip())
        else:
            i += 1
    return items


def _narrative_sections(narrative: str) -> dict:
    """Map each `## `-headed subsection name (lower-cased) → its body text,
    HTML comments stripped."""
    text = re.sub(r"<!--.*?-->", "", narrative, flags=re.S)
    out: dict[str, str] = {}
    for m in re.finditer(r"^##\s+(.*?)\s*\n(.*?)(?=\n##\s|\Z)", text, re.S | re.M):
        out[m.group(1).strip().lower()] = m.group(2)
    return out


def _focus_text_and_gates(focus: str) -> tuple[str, list[dict]]:
    """Split Current-focus into prose (→ <br><br>-joined inline HTML, the form
    the populator wraps in <p>…</p>) and the gate table (→ gate dicts). All text
    is escape()'d before inline markup, so only the generator's own tags survive
    into the innerHTML sink."""
    lines = focus.splitlines()
    prose: list[str] = []
    gates: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if (line.lstrip().startswith("|") and i + 1 < n
                and re.match(r"\s*\|[\s:|-]+\|?\s*$", lines[i + 1])):
            i += 2
            while i < n and lines[i].lstrip().startswith("|"):
                c = _cells(lines[i])
                if len(c) >= 3:
                    gates.append({
                        "name": _inline(escape(re.sub(r"\*\*", "", c[0]).strip())),
                        "status": _inline(escape(c[1])),
                        "pointer": _inline(escape(c[2])),
                        "stateClass": _gate_state_class(c[1]),
                    })
                i += 1
        else:
            prose.append(line)
            i += 1
    paras: list[str] = []
    buf: list[str] = []
    for ln in prose:
        if ln.strip():
            buf.append(ln.strip())
        elif buf:
            paras.append(" ".join(buf))
            buf = []
    if buf:
        paras.append(" ".join(buf))
    text = "<br><br>".join(_inline(escape(p)) for p in paras)
    return text, gates


def _parse_start_here(section: str) -> list[dict]:
    """Each Start-here bullet `**<type> · <track>:** <prose>` → a structured
    item: type/track feed a CSS class + label, prose is an innerHTML sink."""
    out = []
    for it in _logical_bullets(section):
        m = re.match(r"\*\*\s*(.+?)\s*:?\s*\*\*\s*(.*)$", it, re.S)
        label, rest = (m.group(1), m.group(2)) if m else ("", it)
        parts = [p.strip() for p in re.split(r"·", label, maxsplit=1)]
        out.append({
            "type": escape(parts[0]) if parts and parts[0] else "",
            "track": escape(parts[1]) if len(parts) > 1 else "",
            "text": _inline(escape(rest.strip())),
        })
    return out


def _narrative_data(narrative: str, warnings: list[str]) -> dict:
    """Decompose the narrative string into the dashboard's sidebar slots.

    A DISPLAY-ONLY projection of the authored narrative — none of it reaches
    next_pointer(), so the AC3 computed-pointer invariant is untouched (the card
    pointer and Start-here may legitimately diverge; see module docstring). The
    staleness/size/completion banners (which this design has no banner slot for)
    are surfaced as a leading note in Current focus."""
    sec = _narrative_sections(narrative)
    text, gates = _focus_text_and_gates(sec.get("current focus", ""))
    if warnings:
        warn = "<br>".join(_inline(escape(w.lstrip("> ").strip()))
                           for w in warnings)
        text = f"{warn}<br><br>{text}" if text else warn
    return {
        "currentFocus": {"text": text, "gates": gates},
        "startHere": _parse_start_here(sec.get("start here next session", "")),
        "openDecisions": [_inline(escape(b)) for b
                          in _logical_bullets(sec.get("open decisions", ""))],
    }


def _track_data(feature: str, done: int, total: int, states: dict,
                issues: list[dict]) -> dict:
    """One track's card + drawer data. The next-pointer is computed PURELY from
    issue state via next_pointer() (AC3) — never from the narrative."""
    frac = done / (total or 1)
    counts = {s: sum(1 for i in issues if states[i["num"]] == s)
              for s in STATE_ORDER}
    chips = [{"label": f"{counts[s]} {STATE_LABEL[s]}", "type": s}
             for s in STATE_ORDER if counts[s]]
    nxt = next_pointer(issues, states)
    if nxt:
        nxt_text, nxt_num, status = _strip_md(nxt["title"]), f"#{nxt['num']}", "in-progress"
    elif total and done == total:
        nxt_text, nxt_num, status = "✓ 전부 완료", "✓", "settled"
    else:
        nxt_text, nxt_num, status = "휴면 · 열린 작업 없음", "휴면", "settled"
    # Escaping boundary (status-harness#07): every free-form field reaching an
    # innerHTML sink must be neutralized, by field semantics:
    #   - innerHTML-only plain-text fields (issue.title, issue.triage) -> escape
    #     at the SOURCE here (issue.title also carries inline HTML from _inline).
    #   - dual-sink fields ALSO read via innerText (track.name, track.next) ->
    #     store RAW here and esc() at the card render site (innerText needs raw).
    rows = []
    for i in issues:
        blk = ", ".join(f"#{b}" for b in i["blockers"]) or "—"
        rows.append({
            "id": i["num"],
            "title": _inline(escape(i["title"])),
            "triage": escape(i["triage"]),
            "criteria": f'{i["done"]}/{i["total"]}',
            "state": states[i["num"]],
            "blockedBy": blk,
        })
    return {
        "name": feature,
        "percent": round(frac * 100),
        "criteria": f"{done}/{total}",
        "status": status,
        "chips": chips,
        "next": nxt_text,
        "nextNum": nxt_num,
        "issues": rows,
    }


def _json_payload(data: dict) -> str:
    """Serialize projectData for embedding inside the inline <script>.

    json.dumps gives valid JS-string-literal quoting but does NOT neutralize
    HTML metacharacters, so a value containing </script> would close the block
    early. Unicode-escape <, >, & (and the JS line terminators U+2028/2029) on
    the dumped string: value-preserving (the engine decodes \\uXXXX back at
    parse time) yet the HTML parser scanning the raw <script> body never sees a
    literal <. This composes with the per-field escape() of Layer 1."""
    s = json.dumps(data, ensure_ascii=False)
    return (s.replace("<", "\\u003c").replace(">", "\\u003e")
             .replace("&", "\\u0026")
             .replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029"))


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project Status</title>
<style>
/* --- Google Fonts --- */

/* --- Design Tokens & Variables --- */
:root {
  --bg-primary: #f8fafc;
  --bg-secondary: #ffffff;
  --bg-tertiary: #f1f5f9;
  
  --text-primary: #0f172a;
  --text-secondary: #334155;
  --text-muted: #64748b;
  
  --border-color: #e2e8f0;
  
  --accent-gradient: linear-gradient(135deg, #4f46e5, #8b5cf6);
  --accent-color: #4f46e5;
  --accent-light: #eff6ff;
  
  --success: #10b981;
  --success-bg: #ecfdf5;
  --success-border: #a7f3d0;
  
  --warning: #f59e0b;
  --warning-bg: #fffbeb;
  --warning-border: #fde68a;
  
  --danger: #ef4444;
  --danger-bg: #fef2f2;
  --danger-border: #fca5a5;
  
  --info: #06b6d4;
  --info-bg: #ecfeff;
  --info-border: #a5f3fc;
  
  --card-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.05), 0 2px 4px -2px rgb(0 0 0 / 0.05);
  --card-hover-shadow: 0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1);
  --transition-speed: 0.25s;
}

[data-theme="dark"] {
  --bg-primary: #0b0f19;
  --bg-secondary: #161f30;
  --bg-tertiary: #1e293b;
  
  --text-primary: #f8fafc;
  --text-secondary: #cbd5e1;
  --text-muted: #94a3b8;
  
  --border-color: #334155;
  
  --accent-gradient: linear-gradient(135deg, #6366f1, #a855f7);
  --accent-color: #6366f1;
  --accent-light: #1e1b4b;
  
  --success: #10b981;
  --success-bg: rgba(16, 185, 129, 0.1);
  --success-border: rgba(16, 185, 129, 0.25);
  
  --warning: #f59e0b;
  --warning-bg: rgba(245, 158, 11, 0.1);
  --warning-border: rgba(245, 158, 11, 0.25);
  
  --danger: #f43f5e;
  --danger-bg: rgba(244, 63, 94, 0.1);
  --danger-border: rgba(244, 63, 94, 0.25);
  
  --info: #06b6d4;
  --info-bg: rgba(6, 182, 212, 0.1);
  --info-border: rgba(6, 182, 212, 0.25);
  
  --card-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.3), 0 2px 4px -2px rgb(0 0 0 / 0.3);
  --card-hover-shadow: 0 12px 20px -3px rgb(0 0 0 / 0.5), 0 4px 8px -4px rgb(0 0 0 / 0.5);
}

/* --- Base Layout --- */
* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  background-color: var(--bg-primary);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  line-height: 1.6;
  transition: background-color var(--transition-speed), color var(--transition-speed);
}

header {
  border-bottom: 1px solid var(--border-color);
  background-color: var(--bg-secondary);
  position: sticky;
  top: 0;
  z-index: 50;
  transition: background-color var(--transition-speed), border-color var(--transition-speed);
}

.header-container {
  max-width: 1600px;
  margin: 0 auto;
  padding: 16px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.brand h1 {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.5px;
  background: var(--accent-gradient);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

.brand-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 99px;
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 16px;
}

.search-wrapper {
  position: relative;
}

.search-input {
  background-color: var(--bg-tertiary);
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  padding: 8px 16px 8px 36px;
  border-radius: 8px;
  font-size: 14px;
  width: 240px;
  transition: width 0.3s, border-color var(--transition-speed);
}

.search-input:focus {
  outline: none;
  border-color: var(--accent-color);
  width: 280px;
}

.search-icon {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-muted);
  pointer-events: none;
}

.theme-toggle-btn {
  background: none;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  cursor: pointer;
  padding: 8px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background-color var(--transition-speed);
}

.theme-toggle-btn:hover {
  background-color: var(--bg-tertiary);
}

/* --- Main Structure --- */
.dashboard-container {
  max-width: 1600px;
  margin: 24px auto;
  padding: 0 24px;
  display: grid;
  grid-template-columns: 420px 1fr;
  gap: 24px;
}

@media (max-width: 1100px) {
  .dashboard-container {
    grid-template-columns: 1fr;
  }
}

.panel-card {
  background-color: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 16px;
  padding: 24px;
  box-shadow: var(--card-shadow);
  transition: background-color var(--transition-speed), border-color var(--transition-speed), box-shadow var(--transition-speed);
  margin-bottom: 24px;
}

/* --- Left Column: Project Overview & Narrative --- */
.sidebar-sticky {
  position: sticky;
  top: 90px;
  max-height: calc(100vh - 120px);
  overflow-y: auto;
  padding-right: 4px;
}

/* Custom Scrollbar for Sidebar */
.sidebar-sticky::-webkit-scrollbar {
  width: 6px;
}
.sidebar-sticky::-webkit-scrollbar-track {
  background: transparent;
}
.sidebar-sticky::-webkit-scrollbar-thumb {
  background: var(--border-color);
  border-radius: 99px;
}

/* Overall Status Widget */
.overall-widget {
  display: flex;
  align-items: center;
  gap: 20px;
}

.circular-progress {
  width: 90px;
  height: 90px;
  position: relative;
}

.circular-progress svg {
  transform: rotate(-90deg);
  width: 90px;
  height: 90px;
}

.circular-progress circle {
  fill: none;
  stroke-width: 8;
}

.circular-progress .bg-ring {
  stroke: var(--bg-tertiary);
}

.circular-progress .fill-ring {
  stroke: var(--accent-color);
  stroke-linecap: round;
  transition: stroke-dashoffset 0.5s ease-in-out;
}

.progress-text {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  font-size: 20px;
  font-weight: 700;
  color: var(--text-primary);
}

.overall-info h3 {
  font-size: 15px;
  font-weight: 500;
  color: var(--text-muted);
  margin-bottom: 4px;
}

.overall-info .stats {
  font-size: 24px;
  font-weight: 700;
  color: var(--text-primary);
}

.overall-info .subtext {
  font-size: 12px;
  color: var(--text-muted);
}

/* Narrative Sections */
.section-title {
  font-size: 15px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-muted);
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.narrative-content {
  font-size: 14px;
  color: var(--text-secondary);
}

.narrative-content p {
  margin-bottom: 12px;
}

.narrative-content code {
  font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Menlo, Consolas, monospace;
  background-color: var(--bg-tertiary);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.9em;
  color: var(--accent-color);
}

/* Gates Pipeline visualization */
.gates-pipeline {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.gate-node {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  position: relative;
}

.gate-node:not(:last-child)::after {
  content: '';
  position: absolute;
  left: 12px;
  top: 26px;
  bottom: -16px;
  width: 2px;
  background-color: var(--border-color);
}

.gate-indicator {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  z-index: 1;
}

.gate-node.shipped .gate-indicator {
  background-color: var(--success-bg);
  color: var(--success);
  border: 2px solid var(--success-border);
}

.gate-node.active .gate-indicator {
  background-color: var(--info-bg);
  color: var(--info);
  border: 2px solid var(--info-border);
}

.gate-node.wontfix .gate-indicator {
  background-color: var(--danger-bg);
  color: var(--danger);
  border: 2px solid var(--danger-border);
}

.gate-node.deferred .gate-indicator {
  background-color: var(--bg-tertiary);
  color: var(--text-muted);
  border: 2px solid var(--border-color);
}

.gate-details {
  flex: 1;
}

.gate-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 2px;
}

.gate-name {
  font-weight: 600;
  font-size: 14px;
}

.gate-status {
  font-size: 11px;
  font-weight: 500;
}

.gate-node.shipped .gate-status { color: var(--success); }
.gate-node.active .gate-status { color: var(--info); }
.gate-node.wontfix .gate-status { color: var(--danger); }
.gate-node.deferred .gate-status { color: var(--text-muted); }

.gate-pointer {
  font-size: 12px;
  color: var(--text-muted);
}

/* Start Here Checklist */
.start-here-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.todo-item {
  display: flex;
  gap: 12px;
  padding: 12px;
  border-radius: 8px;
  background-color: var(--bg-tertiary);
  font-size: 13.5px;
  border-left: 4px solid var(--border-color);
}

.todo-item.active { border-left-color: var(--accent-color); }
.todo-item.dormant { border-left-color: var(--text-muted); }
.todo-item.parallel { border-left-color: var(--info); }

.todo-badge {
  font-size: 10px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 4px;
  align-self: flex-start;
  text-transform: uppercase;
}

.todo-item.active .todo-badge { background: var(--accent-light); color: var(--accent-color); }
.todo-item.dormant .todo-badge { background: var(--border-color); color: var(--text-muted); }
.todo-item.parallel .todo-badge { background: var(--info-bg); color: var(--info); }

.todo-text {
  flex: 1;
  color: var(--text-secondary);
}

.todo-text strong {
  color: var(--text-primary);
}

.todo-track {
  display: block;
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
  font-weight: 500;
}

/* Open Decisions Card */
.decision-item {
  padding: 12px;
  border-radius: 8px;
  background-color: var(--warning-bg);
  border: 1px dashed var(--warning-border);
  color: var(--text-secondary);
  font-size: 13.5px;
}

/* --- Right Column: Grid and Cards --- */
.main-content {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.tabs-wrapper {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  flex-wrap: wrap;
  gap: 12px;
}

.filter-tabs {
  display: flex;
  background-color: var(--bg-secondary);
  border: 1px solid var(--border-color);
  padding: 4px;
  border-radius: 10px;
  gap: 4px;
}

.tab-btn {
  background: none;
  border: none;
  color: var(--text-secondary);
  font-size: 14px;
  font-weight: 500;
  padding: 8px 16px;
  border-radius: 8px;
  cursor: pointer;
  transition: all var(--transition-speed);
}

.tab-btn:hover {
  background-color: var(--bg-tertiary);
  color: var(--text-primary);
}

.tab-btn.active {
  background-color: var(--accent-color);
  color: #ffffff;
}

.tracks-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
  gap: 20px;
}

/* Cards Design */
.track-card {
  background-color: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 16px;
  padding: 20px;
  box-shadow: var(--card-shadow);
  display: flex;
  flex-direction: column;
  height: 100%;
  position: relative;
  overflow: hidden;
  transition: transform var(--transition-speed), box-shadow var(--transition-speed), border-color var(--transition-speed), background-color var(--transition-speed);
}

.track-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4px;
  background: var(--border-color);
  transition: background var(--transition-speed);
}

.track-card.active-track::before {
  background: var(--accent-gradient);
}

.track-card.settled-track::before {
  background: var(--success);
}

.track-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--card-hover-shadow);
  border-color: var(--accent-color);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.track-name-wrapper {
  display: flex;
  align-items: center;
  gap: 8px;
}

.track-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.3px;
}

.pct-badge {
  font-size: 13px;
  font-weight: 700;
  color: var(--accent-color);
  background: var(--accent-light);
  padding: 2px 8px;
  border-radius: 6px;
}

.track-card.settled-track .pct-badge {
  color: var(--success);
  background: var(--success-bg);
}

/* Neon Mini Progress Bar */
.progress-bar-container {
  height: 6px;
  background-color: var(--bg-tertiary);
  border-radius: 99px;
  overflow: hidden;
  margin-bottom: 8px;
  position: relative;
}

.progress-bar-fill {
  height: 100%;
  background: var(--accent-gradient);
  border-radius: 99px;
  transition: width 0.5s ease-in-out;
}

.track-card.settled-track .progress-bar-fill {
  background: var(--success);
}

.criteria-text {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 16px;
  font-weight: 500;
}

/* Chip sets */
.chips-container {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 20px;
}

.card-chip {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 99px;
  border: 1px solid var(--border-color);
  color: var(--text-muted);
}

.card-chip.done {
  color: var(--success);
  border-color: var(--success-border);
  background-color: var(--success-bg);
}

.card-chip.in-progress {
  color: var(--info);
  border-color: var(--info-border);
  background-color: var(--info-bg);
}

.card-chip.todo {
  color: var(--accent-color);
  border-color: var(--border-color);
}

.card-chip.blocked, .card-chip.wontfix {
  color: var(--danger);
  border-color: var(--danger-border);
  background-color: var(--danger-bg);
}

.card-chip.parked {
  color: var(--warning);
  border-color: var(--warning-border);
  background-color: var(--warning-bg);
}

/* Next Action Box */
.next-box {
  margin-top: auto;
  border-radius: 10px;
  background-color: var(--bg-tertiary);
  padding: 12px 14px;
  border-left: 4px solid var(--accent-color);
  font-size: 13.5px;
  transition: background-color var(--transition-speed);
}

.track-card.settled-track .next-box {
  border-left-color: var(--success);
  background-color: var(--success-bg);
}

.next-box-header {
  font-size: 11px;
  font-weight: 700;
  color: var(--accent-color);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.track-card.settled-track .next-box-header {
  color: var(--success);
}

.next-content {
  color: var(--text-secondary);
  font-weight: 500;
  display: flex;
  align-items: flex-start;
}

.next-num {
  font-weight: 700;
  color: var(--accent-color);
  margin-right: 6px;
  font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Menlo, Consolas, monospace;
}

.track-card.settled-track .next-num {
  color: var(--success);
}

/* View Details Button */
.view-details-btn {
  margin-top: 14px;
  width: 100%;
  background: none;
  border: 1px solid var(--border-color);
  padding: 8px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: all var(--transition-speed);
}

.view-details-btn:hover {
  background-color: var(--bg-tertiary);
  border-color: var(--accent-color);
  color: var(--accent-color);
}

/* --- Drawer (Slide-out panel) --- */
.drawer-backdrop {
  position: fixed;
  inset: 0;
  background-color: rgba(15, 23, 42, 0.4);
  backdrop-filter: blur(4px);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.3s ease;
  z-index: 999;
}

.drawer-backdrop.active {
  opacity: 1;
  pointer-events: auto;
}

.drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: 680px;
  max-width: 95%;
  background-color: var(--bg-secondary);
  box-shadow: -10px 0 25px -5px rgba(0, 0, 0, 0.1), -4px 0 10px -5px rgba(0, 0, 0, 0.1);
  transform: translateX(100%);
  transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), background-color var(--transition-speed);
  z-index: 1000;
  display: flex;
  flex-direction: column;
}

.drawer.active {
  transform: translateX(0);
}

.drawer-header {
  padding: 24px;
  border-bottom: 1px solid var(--border-color);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.drawer-title-group h2 {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
}

.drawer-title-group p {
  font-size: 13px;
  color: var(--text-muted);
}

.close-btn {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 8px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background-color var(--transition-speed), color var(--transition-speed);
}

.close-btn:hover {
  background-color: var(--bg-tertiary);
  color: var(--text-primary);
}

.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}

.drawer-meta-section {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 24px;
}

.meta-widget {
  background-color: var(--bg-tertiary);
  padding: 16px;
  border-radius: 12px;
  border: 1px solid var(--border-color);
}

.meta-widget-title {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}

.meta-widget-value {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}

/* Issues Table */
.issues-table-container {
  border: 1px solid var(--border-color);
  border-radius: 12px;
  overflow: hidden;
  background-color: var(--bg-secondary);
}

.issues-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.issues-table th, .issues-table td {
  padding: 12px 16px;
  text-align: left;
  border-bottom: 1px solid var(--border-color);
}

.issues-table th {
  background-color: var(--bg-tertiary);
  font-weight: 600;
  color: var(--text-secondary);
}

.issues-table tr:last-child td {
  border-bottom: none;
}

.issues-table tbody tr {
  transition: background-color 0.15s;
}

.issues-table tbody tr:hover {
  background-color: var(--bg-tertiary);
}

.issue-id {
  font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Menlo, Consolas, monospace;
  font-weight: 600;
  color: var(--text-muted);
}

.issue-title {
  font-weight: 500;
  color: var(--text-primary);
}

.issue-title code {
  font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Menlo, Consolas, monospace;
  background-color: var(--bg-tertiary);
  padding: 1px 4px;
  border-radius: 4px;
  font-size: 0.95em;
  color: var(--accent-color);
}

.issue-triage {
  font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--text-muted);
  background-color: var(--bg-tertiary);
  padding: 2px 6px;
  border-radius: 4px;
  display: inline-block;
}

.issue-criteria {
  font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Menlo, Consolas, monospace;
  color: var(--text-muted);
  font-size: 12px;
}

.issue-state-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 500;
}

.issue-blocker {
  font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Menlo, Consolas, monospace;
  font-size: 12px;
  color: var(--text-muted);
}

/* Footer Section */
.footer-text {
  text-align: center;
  padding: 40px 0;
  font-size: 12px;
  color: var(--text-muted);
}

/* Animations */
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

.fade-in-item {
  animation: fadeIn 0.4s ease forwards;
}
</style>
</head>
<body>

<header>
  <div class="header-container">
    <div class="brand">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 2L2 22H22L12 2Z" fill="url(#brandGrad)"/>
        <defs>
          <linearGradient id="brandGrad" x1="2" y1="22" x2="22" y2="2">
            <stop stop-color="#4f46e5"/>
            <stop offset="1" stop-color="#8b5cf6"/>
          </linearGradient>
        </defs>
      </svg>
      <h1>Project Status</h1>
      <span class="brand-badge">STATUS HARNESS</span>
    </div>
    
    <div class="toolbar">
      <div class="search-wrapper">
        <input type="text" id="searchInput" class="search-input" placeholder="트랙 및 이슈 검색...">
        <svg class="search-icon" width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path>
        </svg>
      </div>
      
      <button id="themeToggle" class="theme-toggle-btn" title="테마 변경">
        <svg id="themeIcon" width="18" height="18" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"></path>
        </svg>
      </button>
    </div>
  </div>
</header>

<main class="dashboard-container">
  <!-- Left Side: Overview & Narrative -->
  <aside class="sidebar-sticky">
    <!-- Overview Widget -->
    <section class="panel-card fade-in-item">
      <div class="overall-widget">
        <div class="circular-progress">
          <svg>
            <circle class="bg-ring" cx="45" cy="45" r="38"></circle>
            <circle class="fill-ring" id="overallRing" cx="45" cy="45" r="38"></circle>
          </svg>
          <div class="progress-text" id="overallPct">97%</div>
        </div>
        <div class="overall-info">
          <h3>전체 기준 충족률</h3>
          <div class="stats" id="overallCriteriaText">657 / 675 AC</div>
          <div class="subtext">__TRACKS_COUNT__개 개발 트랙 동기화 완료</div>
        </div>
      </div>
    </section>

    <!-- Current Focus -->
    <section class="panel-card fade-in-item" style="animation-delay: 0.1s;">
      <h2 class="section-title">
        <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path>
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path>
        </svg>
        Current Focus
      </h2>
      <div class="narrative-content" id="currentFocusNarrative">
        <!-- JS-populated -->
      </div>
      <div class="gates-pipeline" id="gatesPipeline">
        <!-- JS-populated -->
      </div>
    </section>

    <!-- Start Here Next Session -->
    <section class="panel-card fade-in-item" style="animation-delay: 0.2s;">
      <h2 class="section-title">
        <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path>
        </svg>
        Start Here Next Session
      </h2>
      <div class="start-here-list" id="startHereList">
        <!-- JS-populated -->
      </div>
    </section>

    <!-- Open Decisions -->
    <section class="panel-card fade-in-item" style="animation-delay: 0.3s;">
      <h2 class="section-title">
        <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
        </svg>
        Open Decisions
      </h2>
      <div style="display: flex; flex-direction: column; gap: 10px;" id="openDecisionsList">
        <!-- JS-populated -->
      </div>
    </section>
  </aside>

  <!-- Right Side: Tracks Board -->
  <div class="main-content">
    <div class="tabs-wrapper fade-in-item">
      <div class="filter-tabs" id="filterTabs">
        <button class="tab-btn active" data-filter="all">전체 트랙</button>
        <button class="tab-btn" data-filter="in-progress">진행 중</button>
        <button class="tab-btn" data-filter="completed">완료됨</button>
      </div>
      <div style="font-size: 13px; color: var(--text-muted); font-weight: 500;" id="filteredCount">
        Showing 9 of 9 tracks
      </div>
    </div>

    <div class="tracks-grid" id="tracksGrid">
      <!-- JS-populated -->
    </div>
    
    <div class="footer-text fade-in-item">
      Generated by the status harness (status.py --html) • Local Time: __GENERATED_AT__
    </div>
  </div>
</main>

<!-- Details & Issues Slide-out Drawer -->
<div class="drawer-backdrop" id="drawerBackdrop"></div>
<div class="drawer" id="issuesDrawer">
  <div class="drawer-header">
    <div class="drawer-title-group">
      <h2 id="drawerTrackName">Track Details</h2>
      <p id="drawerTrackCriteria">0/0 criteria met</p>
    </div>
    <button class="close-btn" id="closeDrawer" title="닫기">
      <svg width="20" height="20" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12"></path>
      </svg>
    </button>
  </div>
  
  <div class="drawer-body">
    <div class="drawer-meta-section">
      <div class="meta-widget">
        <div class="meta-widget-title">Next Action</div>
        <div class="meta-widget-value" id="drawerNextAction" style="font-size:14px; font-weight:500;">—</div>
      </div>
      <div class="meta-widget">
        <div class="meta-widget-title">Status Summary</div>
        <div class="meta-widget-value" id="drawerStatusSummary" style="font-size:14px; font-weight:500;">—</div>
      </div>
    </div>
    
    <div class="section-title">
      <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"></path>
      </svg>
      Issues & Acceptance Criteria
    </div>
    
    <div class="issues-table-container">
      <table class="issues-table">
        <thead>
          <tr>
            <th width="45">#</th>
            <th>Issue Title</th>
            <th width="110">Triage</th>
            <th width="70">AC</th>
            <th width="100">State</th>
            <th width="85">Blocker</th>
          </tr>
        </thead>
        <tbody id="drawerIssuesList">
          <!-- JS-populated -->
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
/* --- Dataset --- */
const projectData = __PROJECT_DATA_JSON__;

/* --- DOM Elements --- */
const tracksGrid = document.getElementById('tracksGrid');
const filterCount = document.getElementById('filteredCount');
const filterTabs = document.getElementById('filterTabs');
const searchInput = document.getElementById('searchInput');
const themeToggle = document.getElementById('themeToggle');
const themeIcon = document.getElementById('themeIcon');
const overallRing = document.getElementById('overallRing');
const overallPctText = document.getElementById('overallPct');
const overallCriteriaText = document.getElementById('overallCriteriaText');

/* Drawer Elements */
const issuesDrawer = document.getElementById('issuesDrawer');
const drawerBackdrop = document.getElementById('drawerBackdrop');
const drawerTrackName = document.getElementById('drawerTrackName');
const drawerTrackCriteria = document.getElementById('drawerTrackCriteria');
const drawerNextAction = document.getElementById('drawerNextAction');
const drawerStatusSummary = document.getElementById('drawerStatusSummary');
const drawerIssuesList = document.getElementById('drawerIssuesList');
const closeDrawerBtn = document.getElementById('closeDrawer');

/* --- Initialization --- */
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  setupOverallProgress();
  populateNarratives();
  renderTracks('all', '');
  
  // Event Listeners
  filterTabs.addEventListener('click', handleFilterClick);
  searchInput.addEventListener('input', handleSearchInput);
  themeToggle.addEventListener('click', toggleTheme);
  closeDrawerBtn.addEventListener('click', closeDrawer);
  drawerBackdrop.addEventListener('click', closeDrawer);
});

/* --- Theme Handling --- */
function initTheme() {
  const savedTheme = localStorage.getItem('theme') || 'dark'; // Default Dark
  document.documentElement.setAttribute('data-theme', savedTheme);
  updateThemeIcon(savedTheme);
}

function toggleTheme() {
  const currentTheme = document.documentElement.getAttribute('data-theme');
  const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', newTheme);
  localStorage.setItem('theme', newTheme);
  updateThemeIcon(newTheme);
}

function updateThemeIcon(theme) {
  if (theme === 'dark') {
    themeIcon.innerHTML = `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364-6.364l-.707.707M6.343 17.657l-.707.707m0-12.728l.707.707m12.728 12.728l.707.707M12 8a4 4 0 100 8 4 4 0 000-8z"></path>`;
    themeToggle.setAttribute('title', '라이트 테마로 변경');
  } else {
    themeIcon.innerHTML = `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"></path>`;
    themeToggle.setAttribute('title', '다크 테마로 변경');
  }
}

/* --- Overall Progress System --- */
function setupOverallProgress() {
  const pct = projectData.summary.percent;
  overallPctText.innerText = `${pct}%`;
  overallCriteriaText.innerText = `${projectData.summary.metCriteria} / ${projectData.summary.totalCriteria} AC`;
  
  // Circular Ring Dash offset
  const radius = 38;
  const circumference = 2 * Math.PI * radius;
  overallRing.style.strokeDasharray = circumference;
  
  const offset = circumference - (pct / 100) * circumference;
  overallRing.style.strokeDashoffset = offset;
}

/* --- Sidebar Narrative Populator --- */
function populateNarratives() {
  // Current Focus Text
  document.getElementById('currentFocusNarrative').innerHTML = `<p>${projectData.narrative.currentFocus.text}</p>`;
  
  // Gates Pipeline SVG/Timeline
  const pipelineContainer = document.getElementById('gatesPipeline');
  pipelineContainer.innerHTML = projectData.narrative.currentFocus.gates.map((gate, i) => `
    <div class="gate-node ${gate.stateClass}">
      <div class="gate-indicator">${i + 1}</div>
      <div class="gate-details">
        <div class="gate-header">
          <span class="gate-name">${gate.name}</span>
          <span class="gate-status">${gate.status}</span>
        </div>
        <div class="gate-pointer">${gate.pointer}</div>
      </div>
    </div>
  `).join('');
  
  // Start Here List
  const startHereContainer = document.getElementById('startHereList');
  startHereContainer.innerHTML = projectData.narrative.startHere.map(item => `
    <div class="todo-item ${getTodoItemClass(item.type)}">
      <div class="todo-badge">${item.type}</div>
      <div class="todo-text">
        ${item.text}
        <span class="todo-track">Track: ${item.track}</span>
      </div>
    </div>
  `).join('');
  
  // Open Decisions List
  const openDecisionsContainer = document.getElementById('openDecisionsList');
  openDecisionsContainer.innerHTML = projectData.narrative.openDecisions.map(decision => `
    <div class="decision-item">
      ${decision}
    </div>
  `).join('');
}

function getTodoItemClass(type) {
  if (type === '능동') return 'active';
  if (type === '휴면') return 'dormant';
  if (type === '병행') return 'parallel';
  return '';
}

/* --- Tracks Grid Builder & Filter --- */
let activeFilter = 'all';
let searchQuery = '';

function handleFilterClick(e) {
  if (!e.target.classList.contains('tab-btn')) return;
  
  // Toggle Active Styling
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  e.target.classList.add('active');
  
  activeFilter = e.target.getAttribute('data-filter');
  renderTracks(activeFilter, searchQuery);
}

function handleSearchInput(e) {
  searchQuery = e.target.value.toLowerCase().trim();
  renderTracks(activeFilter, searchQuery);
}

function renderTracks(filter, query) {
  tracksGrid.innerHTML = '';
  
  const filtered = projectData.tracks.filter(track => {
    // 1. Filter match
    let matchFilter = true;
    if (filter === 'in-progress') {
      matchFilter = track.status === 'in-progress';
    } else if (filter === 'completed') {
      matchFilter = track.percent === 100;
    }
    
    // 2. Search query match
    let matchQuery = true;
    if (query) {
      const matchTrackName = track.name.toLowerCase().includes(query);
      const matchNextText = track.next.toLowerCase().includes(query);
      const matchIssues = track.issues.some(issue => 
        issue.title.toLowerCase().includes(query) || 
        issue.id.includes(query)
      );
      matchQuery = matchTrackName || matchNextText || matchIssues;
    }
    
    return matchFilter && matchQuery;
  });
  
  filterCount.innerText = `Showing ${filtered.length} of ${projectData.tracks.length} tracks`;
  
  if (filtered.length === 0) {
    tracksGrid.innerHTML = `
      <div style="grid-column: 1/-1; text-align: center; padding: 60px 0; color: var(--text-muted);">
        검색 결과와 매칭되는 트랙이 없습니다.
      </div>
    `;
    return;
  }
  
  filtered.forEach((track, index) => {
    const card = document.createElement('div');
    card.className = `track-card fade-in-item ${track.percent === 100 ? 'settled-track' : 'active-track'}`;
    card.style.animationDelay = `${index * 0.05}s`;
    
    const isCompleted = track.percent === 100;
    
    card.innerHTML = `
      <div class="card-header">
        <div class="track-name-wrapper">
          <span class="track-title">${esc(track.name)}</span>
        </div>
        <span class="pct-badge">${track.percent}%</span>
      </div>
      
      <div class="progress-bar-container">
        <div class="progress-bar-fill" style="width: ${track.percent}%"></div>
      </div>
      
      <div class="criteria-text">${track.criteria} Criteria Met</div>
      
      <div class="chips-container">
        ${track.chips.map(chip => `
          <span class="card-chip ${chip.type}">${chip.label}</span>
        `).join('')}
      </div>
      
      <div class="next-box">
        <div class="next-box-header">
          <svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M13 5l7 7-7 7M5 5l7 7-7 7"></path>
          </svg>
          ${isCompleted ? 'Finished' : 'Next Action'}
        </div>
        <div class="next-content">
          ${track.nextNum !== '✓' && track.nextNum !== '휴면' ? `<span class="next-num">${track.nextNum}</span>` : ''}
          <span>${esc(track.next)}</span>
        </div>
      </div>
      
      <button class="view-details-btn">
        <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h7"></path>
        </svg>
        상세 이슈 보기 (${track.issues.length})
      </button>
    `;
    
    tracksGrid.appendChild(card);
    card.querySelector('.view-details-btn').addEventListener('click', () => openTrackDetails(track));
  });
}

/* --- Drawer (Slide-out panel) Action --- */
function openTrackDetails(track) {
  if (!track) return;
  
  drawerTrackName.innerText = track.name;
  drawerTrackCriteria.innerText = `${track.criteria} Acceptance Criteria Met (${track.percent}%)`;
  drawerNextAction.innerText = track.next;
  
  const statsText = track.chips.map(c => c.label).join(' · ');
  drawerStatusSummary.innerText = statsText;
  
  // Render Issues Table Rows
  const tbody = document.getElementById('drawerIssuesList');
  tbody.innerHTML = track.issues.map(issue => {
    return `
      <tr>
        <td class="issue-id">${issue.id}</td>
        <td>
          <div class="issue-title">${issue.title}</div>
        </td>
        <td><span class="issue-triage">${issue.triage}</span></td>
        <td class="issue-criteria">${issue.criteria}</td>
        <td>
          <span class="issue-state-badge">
            ${getStateEmoji(issue.state)}
            <span style="font-size:12px; color: ${getStateColor(issue.state)}">${issue.state}</span>
          </span>
        </td>
        <td class="issue-blocker">${issue.blockedBy}</td>
      </tr>
    `;
  }).join('');
  
  // Slide out Drawer
  issuesDrawer.classList.add('active');
  drawerBackdrop.classList.add('active');
  document.body.style.overflow = 'hidden'; // Lock Scroll
}

function closeDrawer() {
  issuesDrawer.classList.remove('active');
  drawerBackdrop.classList.remove('active');
  document.body.style.overflow = ''; // Unlock Scroll
}

/* HTML-escape a free-form value before it reaches an innerHTML sink. */
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* Helper functions for Triage State UI */
function getStateEmoji(state) {
  if (state === 'done') return '✅';
  if (state === 'in-progress') return '🔵';
  if (state === 'todo') return '⬜';
  if (state === 'wontfix') return '🚫';
  if (state === 'parked') return '⏸️';
  return '—';
}

function getStateColor(state) {
  if (state === 'done') return 'var(--success)';
  if (state === 'in-progress') return 'var(--info)';
  if (state === 'wontfix') return 'var(--danger)';
  if (state === 'parked') return 'var(--warning)';
  return 'var(--text-muted)';
}
</script>
</body>
</html>
"""


def build_dashboard_data(narrative: str, warnings: list[str],
                         feature_data: list[tuple], total_done: int,
                         total_crit: int) -> dict:
    """The dashboard's projectData contract, as a plain dict — the pure data
    seam behind render_html. Tests assert on this dict; the template fill
    stays a mechanical substitution."""
    frac = total_done / (total_crit or 1)
    return {
        "summary": {
            "totalCriteria": total_crit,
            "metCriteria": total_done,
            "percent": round(frac * 100),
            "tracksCount": len(feature_data),
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "description": "Generated by the status harness (status.py --html) "
                           "— human glance view. Source of truth is the issue "
                           "files; never hand-edit.",
        },
        "narrative": _narrative_data(narrative, warnings),
        "tracks": [_track_data(*fd) for fd in feature_data],
    }


def render_html(narrative: str, warnings: list[str],
                feature_data: list[tuple], total_done: int,
                total_crit: int) -> str:
    """Build the single self-contained STATUS.html (gemini glance design,
    status-harness#07). The page renders client-side from an embedded
    projectData literal; build_dashboard_data fills that literal from live
    issue state, the markup/CSS/JS stays verbatim, and the run-time footer
    values are injected. Inline only — no external font/CSS/JS dependency
    (ADR-0031 self-containment)."""
    data = build_dashboard_data(narrative, warnings, feature_data,
                                total_done, total_crit)
    return (DASHBOARD_TEMPLATE
            .replace("__PROJECT_DATA_JSON__", _json_payload(data))
            .replace("__GENERATED_AT__", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__TRACKS_COUNT__", str(len(feature_data))))


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
        _journal("skip", "producer")
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
        _journal("skip", "not-issue-repo", repo=str(root))
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
    mode = "html" if want_html else ("brief" if brief else "stop")
    _journal("fire" if content != old else "pass",
             mode + (":regenerated" if content != old else ":unchanged"),
             repo=str(root))
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
