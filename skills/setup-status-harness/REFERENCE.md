# STATUS harness — reference

## Architecture: two layers

The harness lives in two layers, each with a separate purpose.

**Global layer** — installed once per machine, shared across all projects:

- `~/.claude/scripts/status.py` — the generator. Project-agnostic and opt-in.
- `~/.claude/settings.json` hooks:
  - **`SessionStart`** runs the generator, then prints `STATUS.md` (when it
    exists) into the session context under `=== Project status board ===`.
  - **`Stop`** runs the generator after every turn, so the table never drifts
    while a session is active.

**Project layer** — committed per repo so the harness survives outside the
local hook environment:

- `<repo>/scripts/status.py` — a **vendored copy** of the global generator.
  GitHub Actions runners cannot reach `~/.claude`, so the workflow needs the
  generator in the repo itself.
- `<repo>/.github/workflows/regen-status.yml` — runs on push to `main`,
  regenerates `STATUS.md` from the merged issue state, and commits the result
  back if it changed. `concurrency: regen-status-<ref>` queues parallel runs;
  `cancel-in-progress: false` prevents race-losing commits.
- `<repo>/.gitignore` excludes `.claude/handoffs/` and `.claude/worktrees/`
  (Claude Code's per-session and per-worktree state — never sharable).

Both layers run the same generator. The local copy keeps each agent's view of
`STATUS.md` fresh during a turn; the CI copy keeps the committed `main` copy
fresh after merges. Drift between the two physical files is detected by the
installer and reported as ⚠ — the user syncs manually if intentional changes
on one side need to propagate.

## Why no commit-time auto-stage hook

Earlier versions of the harness had a `PreToolUse` hook on `git commit` that
regenerated `STATUS.md` and auto-staged it into every commit. This guaranteed
`STATUS.md` matched any branch's view but made it **hostile to parallel
worktrees**: two worktrees editing different issues would each carry a
different regenerated `STATUS.md` in their commits, producing a false merge
conflict on every PR even when the underlying issue files did not conflict.

The current design solves that by **never committing `STATUS.md` from a
worktree branch**: the local Stop hook still regenerates the file so the
agent's in-session view is correct, but the auto-stage is gone, so the file
stays out of PR diffs. The CI workflow on `main` is the only writer that
commits `STATUS.md`. There is a ~30s–1min window after merge when `main`'s
`STATUS.md` is stale (the workflow is still running); the trade-off is no
multi-worktree merge conflicts ever.

**No-remote / pre-CI caveat (status-harness#05):** because CI is the sole
committer of `STATUS.md`, a repo with **no GitHub remote** (or before its first
push to `main`) never runs the workflow — so the **first** `STATUS.md` needs a
one-time manual `git add STATUS.md && git commit` to land on `main`. After that,
the regen workflow's commit gate stages-then-checks (`git add` + `git diff
--cached --quiet`), so it correctly commits even that first untracked board once
CI is active, and never makes an empty commit when nothing changed.

## STATUS.md anatomy

Two regions with different owners:

- **Mechanical sections** — progress bar, issue table, derived-state legend,
  optional staleness banner. Fully regenerated every run. Never hand-edit.
- **Narrative block** — between `<!-- narrative:start -->` and
  `<!-- narrative:end -->`. Preserved verbatim across runs. Edited only by a
  human or the `/status` skill. Holds *Current focus*, *Start here next
  session*, *Open decisions*.

A `> ⚠️` banner appears above the narrative when it is still the unedited
template, or when *Start here next session* names only done/missing issues.

## Issue file format

Issues live at `.scratch/<feature>/issues/<NN>-<slug>.md`. The generator's
parser reads four things — issue files must match this shape:

- **Number** — leading digits of the filename (`01-tracer-bullet.md` → `01`).
- **Title** — the first `# ` heading.
- **Triage** — a line `Status: <label>` (e.g. `ready-for-agent`).
- **Acceptance criteria** — checkboxes under a `## Acceptance criteria`
  heading: `- [ ]` (open) and `- [x]` (done) are counted. The heading match
  is exact (case-insensitive), so modifications like
  `## Acceptance criteria (v2)` silently break the count — keep the heading
  literal.
- **Blockers** — bullets under a `## Blocked by` heading of the form
  `- Issue 03 (...)`. Only that bullet shape counts; prose mentions of issue
  numbers are ignored.

Minimal conforming issue:

```markdown
# Random-move tracer bullet

Status: ready-for-agent

## Acceptance criteria

- [ ] First criterion
- [ ] Second criterion

## Blocked by

- Issue 01 (the thing this depends on)
```

Creating issues is **not** this skill's job — use `/triage` (one issue) or
`/to-issues` (break a plan into issues). They produce conforming files.

## Derived state

Per issue, computed from criteria + blockers:

- 0 criteria → `unknown`
- all criteria checked → `done`
- some checked → `in-progress` (this short-circuits the blocker check; a
  partially-done issue reads as in-progress even if its blocker is unfinished)
- none checked, with an unfinished blocker → `blocked`
- otherwise → `todo`

Blocker resolution recurses and is scoped per feature directory; blocker
cycles are broken rather than recursed infinitely.

## CI activation checklist

When the installer creates `.github/workflows/regen-status.yml`, the workflow
will not actually push back to `main` until repo permissions allow it. After
the first push to GitHub:

1. Repo → Settings → Actions → General → **Workflow permissions**
2. Choose **Read and write permissions**
3. Save

The first PR that merges to `main` after that triggers the workflow. The
job typically takes 20–40 seconds; expect the merge commit and the regen
commit to appear back-to-back.
