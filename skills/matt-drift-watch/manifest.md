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

- **caveman — the `disable-model-invocation: true` frontmatter line.** Our only authored
  edit. The trigger-phrase `description` block around it is **upstream-era content**
  (`62f43a1`), not ours — a 2026-08 drift session mis-attributed the description as our
  delta once; the history settles it, don't re-litigate. Upstream later added the same
  line itself (`221ffca`) and then deleted the skill (`7d3ada9`, 2026-05-31), so caveman
  scans **ORPHANED**. Decision: **keep** — it still works locally (`/caveman`; currently
  `off` via settings skillOverrides).

Hazard this section guards against, learned the hard way: prototype's
`disable-model-invocation: true` looked like ours but **was not** — upstream
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
- **code-review / research / to-spec / to-tickets / wayfinder** (engineering, all `NEW`,
  reviewed 2026-08-05) — each duplicates a harness organ we already own or serves Matt's
  idea→ship flow: `code-review` (two-axis diff review) overlaps merge-gate + the validator
  lane; `research` (background reading agent) our dispatch patterns; `to-spec`/`to-tickets`
  are the renamed successors of `to-prd`/`to-issues` (`386d4ff` — our rented copies now
  scan ORPHANED; see the pinned-for-now entry below); `wayfinder` (multi-session planning
  maps) our issue-tracker + STATUS harness. Not adopted.
- **tdd/SKILL.md — pinned.** Upstream `e81f976`+`80e9dcc` collapsed the
  Planning/Tracer-bullet/Loop/Refactor workflow to three bullets and moved refactoring to
  the review stage (`code-review` — a skill we don't adopt, see above). Our copy keeps the
  §4 Refactor stage, the per-cycle checklist, and `refactoring.md` (upstream-deleted at
  `80e9dcc`, so it shows as local-only — expected). `tests.md` **tracks upstream**
  (reflected through `2ab9580`). Revisit only if we adopt `code-review`.
- **`agents/openai.yaml` in every upstream skill dir** (`697d4ce`) — Codex CLI packaging
  metadata; our harness consumes `SKILL.md` (+ referenced docs) only. The scan's per-skill
  `Only in <cache>/…: agents` line is expected every run, not drift. Never reflect.
- **zoom-out — ORPHANED, keep.** Upstream-removed (`e112a6b`) with no successor; ADR-0032
  already pins it alongside caveman. Zero tracking cost.
- **to-prd / to-issues / handoff / setup-matt-pocock-skills — pinned for now**
  (2026-08-05 review; prototype was reflected instead). One coupled decision: adopting the
  renamed successor pair `to-spec`/`to-tickets` would sweep spec/tickets vocabulary through
  owned docs (CONTEXT.md, issue-tracker.md, setup-status-harness) and needs a
  template-authority line in `docs/agents/issue-tracker.md` — to-tickets' inline local
  template drops the `## Acceptance criteria` / `## Blocked by` headings `status.py`
  parses, so as-is adoption breaks the progress bar and blocked-state detection.
  `handoff`'s one-word diff (PRDs→specs) and setup's rewrite (to-tickets/to-spec/wayfinder
  references) are follow-ons of the same rename, so they wait with it. Leaning **adopt
  all** later as one small issue; until then their DIFFERS/ORPHANED lines are expected,
  not drift.
