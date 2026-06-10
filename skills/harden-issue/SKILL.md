---
name: harden-issue
description: Deepen a single markdown issue file via grilling — extends /grill-with-docs with a strict contract that the issue file itself must end up updated (sharpened AC checkboxes, decomposed invariants, ADR spin-off when warranted). Use when the user wants to harden one specific issue before starting work, says "이슈 하나 견고하게", "이슈 강화해줘", "harden this issue", "deepen this issue", invokes /harden-issue, or points at a `.scratch/<feature>/issues/<NN>-*.md` file and wants its AC sharpened.
---

# harden-issue

Thin wrapper over [grill-with-docs](../grill-with-docs/SKILL.md) specialised for the
"deepen one issue" use case. Trial sessions showed `grill-with-docs` alone produces
strong analysis but inconsistently lands it in the issue file — sometimes auto-editing,
sometimes leaving the user to transcribe a Plan diff by hand. This skill closes that gap.

## Inputs

**Required:** path to the issue file, e.g. `.scratch/playlike-chess/issues/01-random-move-tracer.md`.

If invoked without a path, ask for one. Do not guess from `STATUS.md` or the current branch.

## Pre-flight

1. Read the issue file in full.
2. Verify it matches the project's issue-tracker convention (read `docs/agents/issue-tracker.md` in the target repo if present):
   - `Status:` line near the top
   - `## Acceptance criteria` section with `- [ ]` / `- [x]` bullets
   - `## Blocked by` section
3. Per grill-with-docs's "Domain awareness", read `CONTEXT.md` / `CONTEXT-MAP.md` and the `docs/adr/` index.
4. Pause the narrative-staleness guard — this skill edits the issue's lifecycle / AC / resolution and may spin off an ADR every turn, all of which would otherwise trip it mid-grilling (see grill-with-docs § Session lifecycle): `python3 ~/.claude/hooks/narrative_guard.py pause 2>/dev/null || true`. Re-arm it at end-of-session (see the checklist).

If the issue file lacks required headings, surface this *before* grilling and ask whether to normalise it first.

## Grilling

Follow [grill-with-docs](../grill-with-docs/SKILL.md) for questioning style, codebase exploration, and CONTEXT/ADR awareness. The five gates below apply on top.

## Five gates (the contract this skill enforces)

### Gate 1 — Issue file is the primary output target

The issue file at the provided path **must end up with a diff** by end of session. Mechanically: `git diff <issue-path>` must be non-empty before you declare done.

**Default behaviour — auto-write.** When grilling settles a decision, edit it into the issue file right then. Don't batch into an end-of-session dump. If you reach the end and notice the file is still unchanged, name the Gate 1 violation explicitly ("the issue file is unchanged — that's a Gate 1 failure"), then write the diff from the conversation before terminating.

**Two exceptions — confirm instead of auto-write:**

1. **Explicit decide-later items.** If grilling parked a question as unresolved (e.g. "decide once we see Phase 2 traffic shape"), do not invent an AC for it. Report it at end-of-session as an open decision, no edit.
2. **Final-round user pushback.** If the user pushed back on your recommendation in the last grilling round and the new shape isn't yet locked, ask once: "Reflect this into AC as `- [ ] X`?" before writing.

Outside those two cases, write the diff yourself — don't ask permission per edit.

### Gate 2 — Decisions land as concrete AC items, not abstract plan text

Every crystallised decision must convert to one or more `- [ ]` bullets under `## Acceptance criteria`. "We'll do X" → `- [ ] X` in the file, not "Step 3: X" in chat.

### Gate 3 — Invariant / edge-case bundles decompose

If grilling surfaces a bundle (e.g. "7 session invariants", "3 error paths"), decompose into individual AC checkboxes — one per invariant. Do not collapse them into a single "handle all session invariants" bullet.

### Gate 4 — ADR / CONTEXT spin-off is explicit

When a decision qualifies as an ADR per grill-with-docs's three-test (hard to reverse, surprising without context, real trade-off), **say so out loud**: "This is an ADR candidate — I'll write `docs/adr/NNNN-<slug>.md` alongside the issue update." Do not quietly keep it in the issue body or, worse, in chat only. Same for `CONTEXT.md` term resolutions.

### Gate 5 — Underprovisioning check

Watch for your own recommendations that cite "MVP scope" or "we can add this later" to shave standard UX, safety, or correctness features. Before proposing such a cut, surface it explicitly: "I'm tempted to defer X citing scope — confirm this is the right trade-off?" (Trial evidence: an auto-queen-on-promotion recommendation that needed user override.)

## End-of-session checklist

Before declaring the session done, verify against the actual file (`git diff <path>`):

- [ ] The issue file has a real diff.
- [ ] New AC items are `- [ ]` checkboxes under `## Acceptance criteria`, not prose elsewhere.
- [ ] Bundles are decomposed into individual items, not collapsed.
- [ ] Any ADR-qualifying decision is either written as `docs/adr/NNNN-*.md` or the user has explicitly declined.
- [ ] No new `[ ]` item silently shaves a standard UX/safety feature without acknowledgement.
- [ ] Narrative posture refreshed with `/status`, then the guard re-armed: `python3 ~/.claude/hooks/narrative_guard.py resume 2>/dev/null || true` (matches the Pre-flight pause).

If any item is unchecked, fix it before ending the session.
