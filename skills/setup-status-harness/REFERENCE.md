# STATUS harness — reference

## How the harness works

`~/.claude/scripts/status.py` regenerates `STATUS.md` for whatever project is
the current working directory. It is **project-agnostic and opt-in**:

- Project root = `git rev-parse --show-toplevel`, falling back to `cwd`.
- It acts only when `.scratch/*/issues/*.md` files exist. Any other repo is a
  silent no-op — which is why the global hooks are harmless everywhere.

It runs from two hooks in `~/.claude/settings.json`:

- **`SessionStart`** — runs the generator, then prints `STATUS.md` (when it
  exists) into the session context under an `=== Project status board ===`
  header.
- **`Stop`** — runs the generator after every turn, so the table never drifts.

It is also invoked by the `/status` skill.

The generator writes only when the output differs byte-for-byte from the
existing file, so an unchanged project produces no diff.

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
  heading: `- [ ]` (open) and `- [x]` (done) are counted.
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
- some checked → `in-progress`
- none checked, with an unfinished blocker → `blocked`
- otherwise → `todo`

Blocker resolution recurses and is scoped per feature directory; blocker
cycles are broken rather than recursed infinitely.
