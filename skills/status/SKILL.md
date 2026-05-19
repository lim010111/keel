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
   commits, issue files that changed, and what the user said. Keep three
   subsections:
   - `## Current focus` — one or two sentences on the active piece of work.
   - `## Start here next session` — the concrete next action(s). Name the
     issue number. If blocked, say on what.
   - `## Open decisions` — unresolved questions worth not forgetting. Remove
     items once decided; an empty list is fine.

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
