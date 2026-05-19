---
name: setup-status-harness
description: Install and wire up the STATUS harness in a project that lacks it — the global status.py generator, the SessionStart/Stop hooks in settings.json, and the .scratch issue-tracker directory structure — then generate the initial STATUS.md. Use when a project has no STATUS.md, the status board is missing or never updates, the user asks to "set up the STATUS harness", "STATUS.md 만들어줘", "상태 보드 붙여줘", "이 프로젝트에 status harness 깔아줘", or when bootstrapping the harness on a fresh clone or a new machine.
---

# Set up the STATUS harness

The STATUS harness keeps a `STATUS.md` board at a project root automatically in
sync with its issue files. It has three parts; this skill ensures all three.

1. **Global infrastructure** — `~/.claude/scripts/status.py` (the generator)
   and two hooks in `~/.claude/settings.json`: a `SessionStart` hook that runs
   the generator and prints `STATUS.md`, and a `Stop` hook that runs it after
   every turn.
2. **Project content** — issue files at `.scratch/<feature>/issues/*.md`. The
   harness is **opt-in**: with no issue files the generator is a silent no-op
   and no `STATUS.md` is produced.
3. **Output** — `STATUS.md` at the project root, regenerated from (2).

This skill installs (1), scaffolds the directory for (2), and triggers (3).
It does **not** create issues — that is the job of `/triage` or `/to-issues`.
See [REFERENCE.md](REFERENCE.md) for harness internals and the issue format.

## Workflow

**1 — Preview the infrastructure install.** Run:
```
python3 ~/.claude/skills/setup-status-harness/scripts/setup_status_harness.py --dry-run
```
This is idempotent and machine-agnostic: it installs `status.py` only if
missing and adds a hook only if no existing hook already runs `status.py`.

**2 — Confirm if it touches global config.** If the dry run shows a `+` line
that adds a hook (it edits the user's global `~/.claude/settings.json`), show
the user those lines and get an explicit go-ahead before applying. If every
line is `✓` (already complete), skip the confirmation.

**3 — Apply.** Re-run the script without `--dry-run`. Report what changed.

**4 — Handle project content.** Check for issue files:
```
ls .scratch/*/issues/*.md
```
- **Issues already exist** → go to step 5; the harness is content-ready.
- **No issues** → scaffold the structure only. Ask the user for a feature
  slug (kebab-case, e.g. the product or milestone name), then create
  `.scratch/<slug>/issues/.gitkeep`. Tell the user the harness is installed
  but **inert** until issues exist, and that `/triage` (single issue) or
  `/to-issues` (break a plan into issues) populates it — the next `Stop` hook
  then generates `STATUS.md` automatically. **Stop here.**

**5 — Generate and verify `STATUS.md`.** Run:
```
python3 ~/.claude/scripts/status.py
```
Confirm `STATUS.md` now exists at the project root. It will carry a
`> ⚠️ Narrative not written yet` banner because the narrative block is still
the template — offer to run `/status` to fill in *Current focus*,
*Start here next session*, and *Open decisions*.

**6 — Report.** One short summary: what infrastructure was installed vs.
already present, whether the harness is now live (STATUS.md generated) or
inert (awaiting first issue), and the suggested next step.

## Notes

- Safe to run on a project that already has the harness — every step is
  idempotent and reports "already present".
- The script never overwrites an existing `~/.claude/scripts/status.py`. If a
  project needs a newer generator, that is a deliberate manual update, out of
  scope here.
- The `SessionStart` hook prints `STATUS.md` into context only when the file
  exists, and the generator no-ops without issue files — so installing the
  global hooks is harmless in every other repo on the machine.
