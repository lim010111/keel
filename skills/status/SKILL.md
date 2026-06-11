---
name: status
description: Refresh STATUS.md — a project's at-a-glance status board. Regenerates the mechanical issue table from .scratch/ and updates the hand-written narrative (Current focus / Start here next session / Open decisions) based on what happened this session. Use when the user asks for project status, "where are we", "어디까지 했지", "현황 정리", or invokes /status.
---

# Refresh the project status board

`STATUS.md` at the project root is the single shared at-a-glance view of
project state, for any project that uses the local-markdown issue tracker
(`.scratch/<feature>/issues/*.md`). It has two parts with different owners:

- **Mechanical sections** (issue table, progress bar, staleness banner) —
  owned by `~/.claude/scripts/status.py`, regenerated every run. Never
  hand-edit.
- **Narrative block** (between `<!-- narrative:start -->` and
  `<!-- narrative:end -->`) — owned by a human or this skill. This holds the
  judgement the issue files cannot express: what is being worked on right now,
  where to resume, and which decisions are still open.

## Steps

1. Run the generator so the table reflects current issue state:
   ```
   python3 ~/.claude/scripts/status.py
   ```
   If it prints nothing, this project has no `.scratch/*/issues/*.md` files —
   tell the user there is no issue tracker here and stop.

2. Read `STATUS.md` at the project root.

3. Update **only the narrative block** by editing `STATUS.md` between the two
   markers. Base the update on this session's actual work — recent git
   commits, issue files that changed, and what the user said. The narrative is
   a **status board, not a changelog** (full rule:
   `docs/agents/issue-tracker.md` § STATUS.md editing rules). Keep three
   subsections:
   - `## Current focus` — posture + the gate table + **one pointer** to the
     active track (issue # + spec path). NOT a per-session story: no commit
     hashes, finding-IDs, or "what this pass caught" narration. That detail
     belongs in the issue's `> **Resolution:**` block.
   - `## Start here next session` — the concrete next action *per active
     track*, not an AC ledger. One line per track, labelled by track — a
     **closed set**: 능동/병행/휴면. A *finished* track's line is **deleted**,
     never relabelled (`완료`/`종료`/`done`… — the narrative guard blocks
     these); a live follow-up action (push, merge, issue to file) stays as an
     능동/병행 line naming that action. Dormant tracks get a one-line "don't
     touch". Overwrite each line every session (never append). *Which* AC is
     next lives in the issue's checkbox order — point at the issue, don't
     mirror its checklist. A commit hash/PR# is fine only as a *live
     actionable handle*. (Full rule: `docs/agents/issue-tracker.md`
     § narrative is a status board.)
   - `## Open decisions` — **unresolved** questions only. Delete each the
     moment it is decided (its outcome moves to the issue/ADR); an empty list
     is fine.
   If you're tempted to append this session's history to *Current focus*,
   append it to the issue's Resolution block instead.

4. Do not invent progress. The narrative reflects reality — if nothing moved
   this session, say so plainly rather than padding it.

5. Report to the user a two-line summary: the progress fraction from the table
   and the "Start here next session" line.

## Notes

- The global `Stop` hook runs `~/.claude/scripts/status.py` automatically at
  session end, so the table never drifts even if this skill is not invoked.
  The skill exists for the narrative — the part automation cannot judge.
- If the user added or removed issues this session, the table picks that up
  automatically; just make sure the narrative still points somewhere valid.
- The generator adds a `> ⚠️` staleness banner above the narrative when it is
  still the unedited template, or when `## Start here next session` points
  only at done/missing issues. Updating the narrative here clears it on the
  next run — that is the banner's whole purpose.
