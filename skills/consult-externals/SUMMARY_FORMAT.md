# Summary format

The inline synthesis you emit back to the Main Session. Keep it short — the Main Session reads the full report files only if it needs more.

## Required shape

```
## External Council Verdict

**Topic:** <one line restating the decision>

**Codex:** <option name> — <one-line reason, distilled from Codex's Reasoning section>
**agy:**   <option name> — <one-line reason, distilled from agy's Reasoning section>

**Agreement:** <yes / partial / no>

**Framing rejected by:** <none / Codex only / agy only / both>
  <if any: one-line reason and what would change their mind>

**Better alternative proposed?** <none / Codex only / agy only / both>
  <if any: one-line description of the proposed alternative(s)>

**Key disagreement (if any):** <one line on what they disagree about and why it matters>

**Unanswered risks:** <merge of both agents' "Risks I cannot evaluate"; one line, or "none">

**Reports:**
- .scratch/council/<TS>-codex.md
- .scratch/council/<TS>-agy.md
```

## Rules

1. **Do not pick a winner.** This skill is a council, not a judge. If the agents disagree, surface that.
2. **Do not paraphrase beyond recognition.** Each agent's one-line reason should still be traceable to a bullet in their `## Reasoning` section.
3. **Always include the file paths.** The Main Session may need to read the full reports.
4. **Failure rows.** If a clean report file starts with `FAILED:`, that agent did not produce a valid evaluation. Write `**Codex:** (failed: <one-line reason from the FAILED marker>)` on its row. Never fabricate a position from a failed run.
5. **Stay under ~15 lines.** If you need more, the agents' reports were too long — fix the prompt next time, not this summary.
6. **Read clean, not raw.** Always synthesize from `<TS>-codex.md` and `<TS>-agy.md`. The `.raw.md` files are for debugging only — do not pull them into the Main Session's context.

## What the Main Session does next

After receiving your synthesis, the Main Session typically:
- Endorses the council if both agreed and the Main agent's tentative pick matches.
- Pauses and re-asks the user if the council disagrees or proposes a new alternative.
- Reads the full reports only when something in the synthesis surprised it.

So your synthesis is a router, not a verdict. Optimize for fast triage.
