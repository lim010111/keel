# matt-drift-watch — manifest

Non-mechanical knowledge the scan can't derive. `scan.sh` finds *what* differs; this file
says *how to read* the difference. Keep it current: a run that discovers a new protected
delta or settles a new adoption decision edits this file.

## Provenance

Which of our skills are rented from Matt, and in what role (ADR-0031/0032). `scan.sh`
discovers the rented set by symlink + upstream presence, so it stays correct as the set
grows — this table is orientation, not the source list.

| skill | upstream dir | role |
|---|---|---|
| grilling | productivity | rented base — gate core |
| domain-modeling | engineering | rented base — gate core |
| codebase-design | engineering | rented base — gate core |
| grill-with-docs | engineering | rented delegator |
| grill-me | productivity | rented delegator |
| diagnosing-bugs, improve-codebase-architecture, resolving-merge-conflicts, tdd, to-issues, to-prd, triage, prototype | engineering | rented (non-gate) |
| handoff, teach, writing-great-skills | productivity | rented (non-gate) |
| setup-matt-pocock-skills | engineering | rented (installer) |

## Protected deltas

Edits we made on purpose. A resync must **not** clobber these. The scan shows them as a
local-side diff — bin them `protected-delta`, never `upstream-ahead`.

_(none currently.)_ Note the hazard this section guards against, learned the hard way:
prototype's `disable-model-invocation: true` looked like ours but **was not** — upstream
carried that line until `850873c` (2026-06-29) removed it to make prototype model-invoked.
We **adopted** that direction, so prototype now tracks upstream with no local delta. The
lesson: a local-side diff is not automatically a delta we authored (see SKILL.md step 2).

## Vendored-own (we own these — not rented)

- **harden-issue** — our own skill, a derivative of `grill-with-docs`. No upstream
  counterpart, so the scan won't match it. When `grill-with-docs` goes upstream-ahead,
  check whether the change should also flow into harden-issue — but harden-issue itself is
  committed by us (claude-config), unlike rented skills.

## Standing decisions

Upstream skills we've evaluated and decided **not** to adopt. The scan flags them `NEW`
every run; this record is why we skip them. Revisit only if the rationale changes.

- **ask-matt** (engineering) — a router over Matt's full idea→ship flow
  (`to-prd`→`to-issues`→`implement`, …). Would misdescribe our decomposed harness. Not adopted.
- **implement** (engineering) — thin PRD/issue executor tied to Matt's `/review` flow. We
  run implementation through our own issue-tracker / merge-gate / work-interval TDD. Not adopted.
