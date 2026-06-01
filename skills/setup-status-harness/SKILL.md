---
name: setup-status-harness
description: Install and wire up the STATUS harness in two layers — global infrastructure (~/.claude status.py generator + SessionStart/Stop hooks) and per-project files (vendored scripts/status.py, .github/workflows/regen-status.yml for main-push regen, .gitignore entries for worktree state, docs/agents/issue-tracker.md agent reference for the AC-checkbox / narrative-block conventions, plus a one-line session-start `STATUS.md`-read pointer wired into AGENTS.md/CLAUDE.md) — then generate the initial STATUS.md. Use when a project has no STATUS.md, the status board is missing or never updates, the user asks to "set up the STATUS harness", "STATUS.md 만들어줘", "상태 보드 붙여줘", "이 프로젝트에 status harness 깔아줘", or when bootstrapping the harness on a fresh clone or a new machine.
---

# Set up the STATUS harness

The STATUS harness keeps a `STATUS.md` board at the project root automatically
in sync with its issue files. It has two layers; this skill installs both.

**Global layer** — shared across every project on this machine:
1. `~/.claude/scripts/status.py` — the generator.
2. Two hooks in `~/.claude/settings.json`: `SessionStart` (runs the generator
   and prints `STATUS.md`) and `Stop` (runs it after every turn).

**Project layer** — committed into each repo that uses the harness:
3. `<repo>/scripts/status.py` — vendored copy so CI can run it (GitHub Actions
   cannot reach `~/.claude`).
4. `<repo>/.github/workflows/regen-status.yml` — regenerates `STATUS.md` after
   each push to `main`, so worktree branches never need to commit it (which
   prevents the merge-conflict storm parallel worktrees would otherwise hit).
5. `<repo>/.gitignore` adds `.claude/handoffs/` and `.claude/worktrees/`.
6. `<repo>/docs/agents/issue-tracker.md` — agent-facing doc explaining the
   issue file contract, STATUS.md editing rules, and the close-an-issue
   procedure. Agents in the repo need this as their reference for the
   AC-checkbox / narrative-block conventions the harness depends on.

**Issue content** — at `.scratch/<feature>/issues/*.md`. The harness is
opt-in: with no issue files the generator is a silent no-op and no `STATUS.md`
is produced. Creating issues is **not** this skill's job — use `/triage` (one
issue) or `/to-issues` (break a plan into issues).

See [REFERENCE.md](REFERENCE.md) for harness internals, the issue format, and
the rationale behind the two-layer split.

## Workflow

**1 — Preview both layers.** From inside the target repo run:
```
python3 ~/.claude/skills/setup-status-harness/scripts/setup_status_harness.py --dry-run
```
Idempotent: each line shows ✓ (already in place), + (will change), or ⚠
(present but differs from template — needs manual review). Outside any git
repo, the project layer is skipped automatically.

**2 — Confirm if anything will change.** If the dry run shows a `+` line that
touches global config (`~/.claude/settings.json`) or adds project files
(`scripts/status.py`, `.github/workflows/regen-status.yml`, `.gitignore`),
show the user those lines and get an explicit go-ahead. If every line is ✓,
skip the confirmation. For ⚠ lines, surface them — the user decides whether
to sync manually.

**3 — Apply.** Re-run the script without `--dry-run`. Report what changed.

**4 — Wire the harness into agent guidance.** Agents need two harness pointers
in the repo's canonical guidance file. Resolve the target file once: **`AGENTS.md`**
if it exists, else **`CLAUDE.md`**, else suggest `/setup-agents-md` first (then
proceed). Both pointers are idempotent — skip either if an equivalent line is
already present.

   a. **`STATUS.md` session-start pointer** — the harness's payoff: the board is
      worthless if agents never read it. Offer to add a one-liner telling agents
      to read `STATUS.md` before starting work; on the user's go-ahead, append it
      near the top / process section, matching the file's language and tone, e.g.:
      > **작업 시작 시 먼저 `STATUS.md` 를 읽을 것** — 현재 능동 트랙, 다음 행동
      > (*Start here next session*), 열린 결정이 거기 정리돼 있다.

      (English: "**Read `STATUS.md` first when starting work** — current active
      track, next action (*Start here next session*), and open decisions live
      there.")
   b. **Issue-tracker conventions import** — if step 3 created (or already had)
      `docs/agents/issue-tracker.md`, the AC-checkbox / narrative conventions
      should be reachable: suggest `@docs/agents/issue-tracker.md` in the same
      file (one line, near the issue-tracker / process section).

These two are the only AGENTS.md / CLAUDE.md edits this skill makes, and (a) is
written **only after the user's go-ahead**. Beyond them do **not** author
guidance content — that belongs to the user and `setup-agents-md`. For (b),
print the suggested `@` line and let the user paste (an import the user may want
to place deliberately).

**5 — Handle project content.** Check for issue files:
```
ls .scratch/*/issues/*.md
```
- **Issues already exist** → go to step 6; the harness is content-ready.
- **No issues** → scaffold the structure only. Ask the user for a feature
  slug (kebab-case, e.g. the product or milestone name), then create
  `.scratch/<slug>/issues/.gitkeep`. Tell the user the harness is installed
  but **inert** until issues exist, and that `/triage` (single issue) or
  `/to-issues` (break a plan into issues) populates it — the next `Stop` hook
  then generates `STATUS.md` automatically. **Stop here.**

**6 — Generate and verify `STATUS.md`.** Run:
```
python3 ~/.claude/scripts/status.py
```
Confirm `STATUS.md` now exists at the project root. It will carry a
`> ⚠️ Narrative not written yet` banner because the narrative block is still
the template — offer to run `/status` to fill in *Current focus*,
*Start here next session*, and *Open decisions*.

**7 — Activate the CI workflow** (only if step 3 created `regen-status.yml`).
Tell the user to enable, in GitHub → repo Settings → Actions → General →
**Workflow permissions**, "Read and write permissions" — otherwise the
workflow cannot push regenerated STATUS.md back to main.

**8 — Report.** One short summary: what was installed vs. already present,
the step-4 guidance wiring (the `STATUS.md`-read pointer added or already
present; the issue-tracker `@import` suggested), whether the harness is now live
(STATUS.md generated) or inert (awaiting first issue), and the suggested next
step.

## Notes

- Safe to re-run — every step is idempotent and reports "already present".
- The script never overwrites an existing global `~/.claude/scripts/status.py`
  or an existing per-project `scripts/status.py`. If a project's vendored
  copy drifts from the global, the script warns; the user syncs manually
  (`cp ~/.claude/scripts/status.py scripts/status.py`).
- The `SessionStart` hook prints `STATUS.md` into context only when the file
  exists, and the generator no-ops without issue files — so installing the
  global hooks is harmless in every other repo on the machine.
- Use `--no-project` to skip the project layer (e.g. when bootstrapping a
  fresh machine from outside any repo).
