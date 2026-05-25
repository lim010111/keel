---
name: consult-externals
description: During a grilling session (e.g. /grill-me, /grill-with-docs), consult two external agents — Codex and agy — in parallel about a specific decision point. Each agent independently thinks about the recommended option, judges it, and proposes a better alternative if one exists. Returns a side-by-side comparison so the Main Session can decide. Use when the user invokes /consult-externals, says "외부 의견 받아줘", "코덱스랑 agy한테 물어봐", "second opinion", or when the Main agent is unsure which option to recommend at a grilling branch.
---

# consult-externals

Two external thinkers (Codex, agy) review a single decision point in parallel. They cannot edit anything — they only think and report back. The Main Session reads the **cleaned** reports and decides.

## When to invoke

- Mid-grilling, before recommending an answer to the user at a hard branch.
- When the Main agent suspects its own recommendation is weak, biased, or missing context.
- The user explicitly asks for a second opinion.

Skip if: the decision is trivial, reversible, or already settled.

## Inputs you need

Gather these from the current grilling context before invoking the external agents:

1. **Topic** — one sentence stating what is being decided.
2. **Context** — the surrounding situation (project, constraints, what was already decided upstream).
3. **Options** — the choices the Main agent is considering, with each option's tradeoffs as understood so far.
4. **Main agent's tentative recommendation** — which option, and why.

If any of these are missing, ask the user (or re-read the grilling transcript) before calling out.

## Workflow

1. **Pick a slug** — `TS=$(date +%Y%m%d-%H%M%S)`.
2. **Extract the session excerpt** — run `python3 scripts/prepare_slice.py <TS>`. It reads the live session JSONL, reconstructs the active path, scans backward for the grilling-start marker (a `Skill` tool_use launching `grill-me` / `grill-with-docs`), and writes the unedited conversational lead-up to `.scratch/council/<TS>-excerpt.md`. Stats are printed as JSON. If `boundary_mode == "fallback"`, the marker was not found and the slice is the last 5 human turns — note this when composing the prompt.
3. **Compose the prompt** — fill in the placeholders in [PROMPT_TEMPLATE.md](PROMPT_TEMPLATE.md): Topic, Context, Options, Tentative recommendation. For the `# Recent session excerpt` section, paste the contents of `.scratch/council/<TS>-excerpt.md` verbatim. Save the assembled prompt to `.scratch/council/<TS>-prompt.md`.
4. **Launch in parallel** — invoke `scripts/run.sh <prompt-file> <TS>` via `Bash(run_in_background=true)`. The script dispatches Codex and agy as background jobs, waits for both, then **extracts a clean final report** from each (see [REFERENCE.md](REFERENCE.md) for the full pipeline).
5. **Wait for the single completion notification.** Do NOT poll or sleep — the script's `wait` means one notification = both reports ready.
6. **Verify the clean reports** before showing anything to the Main Session:
   - Read `.scratch/council/<TS>-codex.md` and `.scratch/council/<TS>-agy.md`.
   - If a file's first line starts with `FAILED:`, that agent did not produce a valid report. Note the failure; do NOT pretend it succeeded.
   - A valid report contains all four required sections (`## Verdict`, `## Reasoning`, `## Per-option assessment`, `## Risks I cannot evaluate`). A fifth section, `## Better alternative`, appears only when the Verdict is `replace with`; its absence is not a failure.
7. **Synthesize for the Main Session** — emit an inline side-by-side summary with the structure shown in [SUMMARY_FORMAT.md](SUMMARY_FORMAT.md). Keep it terse; the Main agent will open the clean files only if needed.
8. **Return control** — do not pick the winner yourself. Hand the synthesis to the Main Session so the user-facing grilling continues.

## File layout

```
.scratch/council/
  <TS>-excerpt.md      # extracted session-JSONL slice (auto-filled by prepare_slice.py)
  <TS>-prompt.md       # shared prompt (caller-assembled, with excerpt embedded)
  <TS>-codex.raw.md    # full Codex stdout (tool trace, drafts) — kept for debugging
  <TS>-agy.raw.md      # full agy stdout — kept for debugging
  <TS>-codex.md        # CLEAN final report — what the Main Session reads
  <TS>-agy.md          # CLEAN final report
```

The clean file is the slice from the **last** `## Verdict` line to EOF. If that slice is missing required sections, the clean file is replaced with a `FAILED:` marker pointing to the raw file.

## Required output (what you give back)

Always end with this exact block, even if one agent failed:

```
## External Council Verdict

**Topic:** <one line>

**Codex:** <picked option> — <one-line reason>
**agy:**   <picked option> — <one-line reason>

**Agreement:** <yes / partial / no>

**Better alternative proposed?** <none / Codex only / agy only / both — describe in one line>

**Reports:** .scratch/council/<TS>-codex.md, .scratch/council/<TS>-agy.md
```

The Main Session will use this block to continue the grilling.

## Failure modes

- **One agent fails or times out** — its clean file starts with `FAILED:`. Report it explicitly in the verdict block (`**Codex:** (failed: <reason>)`). Do not retry silently. The surviving report is still useful.
- **Both fail** — say so. Do not fabricate a council outcome.
- **Reports disagree sharply** — do NOT pick a winner or paraphrase one toward the other. Surface the disagreement; that is the signal the user needs.
- **An agent tries to do work** (edit files, run tests) — Codex is locked to `--sandbox read-only`; agy is instructed in-prompt to stay read-only. If an agent reports having modified state anyway, flag it in the verdict.
- **An agent's raw output ignored the section contract** — extraction will catch this and mark the clean file `FAILED: ... missing required sections`. Show that, not the raw.

## Files

- [PROMPT_TEMPLATE.md](PROMPT_TEMPLATE.md) — the prompt sent to both agents
- [SUMMARY_FORMAT.md](SUMMARY_FORMAT.md) — required shape of the inline synthesis
- [REFERENCE.md](REFERENCE.md) — CLI flags, extraction pipeline, slice policy, manual fallback
- [DESIGN.md](DESIGN.md) — rationale for the council shape, parallel `wait`, raw/clean split, JSONL slice, etc.
- [scripts/prepare_slice.py](scripts/prepare_slice.py) — JSONL slice extractor (marker + fallback)
- [scripts/run.sh](scripts/run.sh) — dispatcher + extractor (parallel + clean)
