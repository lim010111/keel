---
name: setup-agents-md
description: Bootstrap the AGENTS.md ↔ CLAUDE.md relationship in a target repo (or a subdirectory). AGENTS.md is the canonical agent guidance — read directly by Codex CLI and antigravity — and CLAUDE.md `@import`s it so Claude Code sees the same content. Use when a repo has no AGENTS.md, when only CLAUDE.md exists and should be migrated, when adding nested AGENTS.md under `src/<context>/`, or when the user asks to "set up AGENTS.md", "wire AGENTS.md and CLAUDE.md", "migrate CLAUDE.md to AGENTS.md", "AGENTS.md 셋업", "AGENTS.md 깔아줘".
---

# Set up the AGENTS.md ↔ CLAUDE.md relationship

`AGENTS.md` is the shared, canonical agent-guidance file. Codex CLI and
antigravity CLI both auto-pick it up, the same way Claude Code reads
`CLAUDE.md`. To keep the two in sync without duplicating content, `CLAUDE.md`
becomes a thin wrapper that `@import`s `AGENTS.md`:

```
# CLAUDE.md
@AGENTS.md
```

This skill installs that wrapper relationship. It does **not** author
project-specific guidance — that is a separate, living concern.

## The four starting states

The script branches on what already exists at the target directory:

| State | AGENTS.md | CLAUDE.md | Action |
|---|---|---|---|
| 1 | absent | absent | Create both (template + wrapper) |
| 2 | absent | present | Move CLAUDE.md content into AGENTS.md, replace CLAUDE.md with wrapper |
| 3 | present | absent | Create CLAUDE.md wrapper only |
| 4 | present | present | If CLAUDE.md already has `@AGENTS.md`: silent no-op. Else: refuse + ⚠ — human merges manually |

State 2 is destructive on CLAUDE.md, but git holds the original, so no `.bak`
file is written.

## Workflow

**1 — Pick the target.** Default = repo root (`git rev-parse --show-toplevel`).
Pass a subdirectory path when the user wants nested guidance under, e.g.,
`src/auth/` — the wrapper and template both go there, and `@AGENTS.md`
resolves relative to the CLAUDE.md beside it.

**2 — Preview.** From inside the target repo run:
```
python3 ~/.claude/skills/setup-agents-md/scripts/setup_agents_md.py --dry-run
# or for a subdir
python3 ~/.claude/skills/setup-agents-md/scripts/setup_agents_md.py --dry-run src/auth
```
Each line shows ✓ (already wired), + (will change), or ⚠ (manual merge
needed). Outside a git repo the script errors out — it relies on
`git rev-parse --show-toplevel`.

**3 — Confirm if anything will change.**
- If every line is ✓ → already wired, stop.
- If only `+` lines for **state 1 or 3** (pure creation) → safe to apply
  without further confirmation.
- If a `+` line says **migrate** (state 2) → show the user that
  `CLAUDE.md` content will be moved into `AGENTS.md` and `CLAUDE.md`
  replaced with the wrapper. Get explicit go-ahead. Mention that the
  original is recoverable via `git`.
- If a `⚠` line (state 4 with conflict) → do **not** apply. Tell the user
  both files have independent content and offer to help merge: pick the
  authoritative content, paste it into `AGENTS.md`, replace `CLAUDE.md`
  with just `@AGENTS.md`. Then re-run the skill — it should now report ✓.

**4 — Apply.** Re-run the same command without `--dry-run`.

**5 — Report.** One short summary: what state the repo was in, what was
written, and a nudge for the user to fill in real content in `AGENTS.md`
(the template carries a placeholder comment).

## Subdirectory expansion

Both Claude Code and Codex CLI walk the directory tree for nested
guidance — `<repo>/src/auth/CLAUDE.md` augments `<repo>/CLAUDE.md` when an
agent is working inside `src/auth/`. Same for `AGENTS.md`. This skill
supports that by accepting a path argument:

```
python3 ~/.claude/skills/setup-agents-md/scripts/setup_agents_md.py src/auth
```

The script enforces that the path is inside the repo. Inside the nested
directory, the wrapper still says `@AGENTS.md` (relative to itself) — no
path rewriting needed.

## Idempotency

Every state's "already wired" path emits only ✓ lines and writes nothing.
Safe to re-run from automation. The wrapper detection is anchored to a
line matching `^@(\.\/)?AGENTS\.md\s*$`, so it tolerates either `@AGENTS.md`
or `@./AGENTS.md`.

## Verification

After applying:
- `AGENTS.md` exists at the target.
- `CLAUDE.md` exists at the target and contains a line `@AGENTS.md`.
- Re-running the script reports `Already wired up — nothing to do.`
- (State 2 only) The previous `CLAUDE.md` content is now the body of
  `AGENTS.md`; `git diff` shows the swap.

## Notes

- The script never edits files outside the target directory. The global
  `~/.claude/` tree is untouched.
- The templates under `templates/` are intentionally minimal — they exist
  to bootstrap the *relationship*, not to dictate content.
- For projects that already use `/setup-status-harness`, this skill is
  complementary: STATUS.md tracks issue state, AGENTS.md tracks agent
  guidance.
