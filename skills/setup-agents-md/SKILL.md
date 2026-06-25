---
name: setup-agents-md
description: Bootstrap and maintain the AGENTS.md ↔ CLAUDE.md relationship across a repo. AGENTS.md is the canonical agent guidance — read directly by Codex CLI and antigravity — and CLAUDE.md `@import`s it so Claude Code sees the same content. Recursive by default: one run wires the repo root plus every nested CLAUDE.md/AGENTS.md (src/, gas/, …) and normalizes cross-links so the whole AGENTS.md graph stays consistent. Use when a repo has no AGENTS.md, when only CLAUDE.md exists and should be migrated, when nested CLAUDE.md files under subdirectories still need wiring, or when the user asks to "set up AGENTS.md", "migrate CLAUDE.md to AGENTS.md", "wire up all nested CLAUDE.md", "AGENTS.md 셋업", "AGENTS.md 깔아줘", "하위 CLAUDE.md 까지 정리".
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

This skill installs and maintains that wrapper relationship — for the root
**and** every nested guidance directory. It does **not** author
project-specific guidance; content is moved verbatim, never rewritten.

## The four states (per directory)

The script branches on what exists at each directory:

| State | AGENTS.md | CLAUDE.md | Action |
|---|---|---|---|
| 1 | absent | absent | Create both (template + wrapper) |
| 2 | absent | present | Move CLAUDE.md content into AGENTS.md, replace CLAUDE.md with wrapper |
| 3 | present | absent | Create CLAUDE.md wrapper only |
| 4 | present | present | If CLAUDE.md already has `@AGENTS.md`: silent no-op. Else: refuse + `⚠` — human merges manually |

State 2 is destructive on CLAUDE.md, but git holds the original, so no `.bak`
file is written.

## Recursive by default

A bare run **sweeps the whole repo** — the sweep root (always planned, so a
fresh repo still bootstraps root guidance) plus every directory that already
holds a `CLAUDE.md` or `AGENTS.md`. Discovery is git-scoped
(`ls-files --cached --others --exclude-standard`), so vendored / generated
trees the repo ignores are skipped for free.

```
python3 ~/.claude/skills/setup-agents-md/scripts/setup_agents_md.py --dry-run
```

- A positional `PATH` scopes the sweep to that subtree (`… src`).
- `--single PATH` operates on exactly one directory — the pre-recursive
  behavior, for when you mean only that dir.

Outside a git repo the script errors out — it relies on
`git rev-parse --show-toplevel`.

## Workflow

**1 — Preview.** Run the dry-run above. Each directory's lines show `✓`
(already wired), `+` (will change / migrate), or `⚠` (manual merge needed),
followed by the cross-link plan and the external-reference report.

**2 — Confirm.** One go-ahead covers the whole sweep:
- Only `✓` lines → already wired, stop.
- `+` lines for **state 1 or 3** (pure creation) → safe to apply.
- Any `+ migrate` line (state 2) → show the user that those `CLAUDE.md` files
  move into `AGENTS.md` and become wrappers; get explicit go-ahead. Mention
  the originals are recoverable via `git`. One confirmation covers all of
  them — they are the same recoverable operation.
- Any `⚠` line (state-4 conflict) → that directory is **skipped**, never
  blocks the rest. Tell the user both files there have independent content;
  offer to merge (pick the authoritative content into `AGENTS.md`, replace
  `CLAUDE.md` with `@AGENTS.md`), then re-run — it should report `✓`.

**3 — Apply.** Re-run without `--dry-run`.

**4 — Report.** One short summary: which directories were wired, how many
cross-links were normalized, and the external-reference list the user must
decide on (below).

## The AGENTS.md graph stays coherent

The skill owns one thing and keeps it consistent: **the AGENTS.md graph** —
the set of `AGENTS.md` files and the links between them. After wiring, it
**edits inside the graph** and **only reports outside it**.

- **Inside (auto-fixed):** path-shaped `CLAUDE.md` cross-references *inside
  AGENTS.md files* — link target `](…/CLAUDE.md)`, link text `[…/CLAUDE.md]`,
  inline-code path `` `…/CLAUDE.md` `` — flip to `AGENTS.md` when their target
  directory is now wired. This runs over **all** wired AGENTS.md, including a
  root that was migrated earlier. Bare-word prose (`broken CLAUDE.md /
  README.md`) and links to a non-wired dir (e.g. a state-4 conflict, whose
  `CLAUDE.md` keeps independent content) are left untouched.
- **Outside (reported, never edited):** references that live in source
  comments, READMEs, `.claude/agents/*.md`, or CI templates still resolve
  (the wrapper exists at the old path) but point at the wrapper, not the
  content. The skill lists them `file:line` and stops there — that blast
  radius is the human's call, because auto-editing arbitrary source/tooling
  across any repo is unsafe.

## Idempotency

Every state's "already wired" path emits only `✓` and writes nothing; the
cross-link pass finds nothing left to flip. Safe to re-run from automation.
Wrapper detection is anchored to a line matching `^@(\.\/)?AGENTS\.md\s*$`, so
it tolerates `@AGENTS.md` or `@./AGENTS.md`.

## Verification

After applying:
- `AGENTS.md` exists at each wired directory; its `CLAUDE.md` contains
  `@AGENTS.md`.
- (State 2) The previous `CLAUDE.md` content is now the body of `AGENTS.md`
  (`git diff` shows the swap); a leading `# CLAUDE.md` title became
  `# AGENTS.md`.
- No `…/CLAUDE.md` link to a wired dir remains inside any `AGENTS.md`.
- Re-running reports `Already wired up — nothing to do.`

## Notes

- The script never edits files outside the discovered directories' `AGENTS.md`
  files. The global `~/.claude/` tree is untouched.
- `plan(target, actions)` is the single-directory planner and is unchanged —
  `harness-doctor`'s `auto_fill` calls it directly and derives consent tiers
  from its `kind` vocabulary (`ok`/`change`/`migrate`/`warn`/`error`).
- The templates under `templates/` are intentionally minimal — they bootstrap
  the *relationship*, not the content.
- Complementary to `/setup-status-harness`: STATUS.md tracks issue state,
  AGENTS.md tracks agent guidance.
