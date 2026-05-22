# keel

> An **engineering-discipline harness** that sits on top of native Claude Code.

A keel is the spine of a ship — the structure that holds a course instead of
drifting with the current. This repo is that spine for a coding agent: a set of
devices that keep it from *drifting on guesswork*. A harness for agentic
engineering, not vibe coding.

keel mirrors one person's `~/.claude` config — but only the **authored** parts.
Third-party skills and plugins aren't vendored here; they're referenced from
[`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md)
([ADR-0002](docs/adr/0002-authored-content-only.md)).

---

## Philosophy

Native Claude Code is capable, but left alone it makes the mistakes LLMs are
prone to — it assumes without checking, pours out implementation before tests,
and edits scope nobody asked for. keel blocks those mistakes *structurally*,
not with good intentions.

- **[`CLAUDE.md`](CLAUDE.md)** — the behavioral norms: state your assumptions
  before coding, keep the implementation minimal, make surgical changes, work
  toward a verifiable goal.
- **TDD enforcement hooks** — the norms don't stay as prose. Write
  implementation before a test and you get blocked.
- **The STATUS board** — refreshed automatically every session, so "where was
  I?" is read from a file instead of guessed.

Norms → enforcement → visibility. Those three layers are keel's skeleton.

---

## What's inside

| Kind | Items |
|---|---|
| Skills | `ai-readiness-cartography`, `audit-and-write-readme`, `ci-setup`, `daily-dev-log`, `daily-token-report`, `session-dev-log`, `setup-status-harness`, `status`, `tech-blog`, `third-party-review` |
| Hooks | `tdd_keyword` · `tdd_guard` · `tdd_mark` · `tdd_verify`, `session_devlog` |
| Scripts | `status.py`, `sound_complete.sh`, `sound_permission.sh` |
| Agents | `ci-researcher`, `korean-context-writer` |
| Config | `CLAUDE.md`, `statusline.sh`, `settings.json` |

These components aren't all independent. The three bundles below **only work
when their pieces are kept together** — install just one and you get half a
feature.

### 1. The TDD enforcement pipeline (hooks)

Four hooks form a pipeline, wired together through session files in
`~/.claude/hooks/.tdd-state/`:

1. `tdd_keyword.py` — `UserPromptSubmit`. Detects TDD keywords in the prompt
   and turns on a sticky **TDD MODE**.
2. `tdd_guard.py` — `PreToolUse(Edit|Write)`. While TDD MODE is on, if no test
   file has been touched yet, it **hard-blocks** the creation of a new
   implementation file.
3. `tdd_mark.py` — `PostToolUse(Edit|Write)`. Records that a test file was
   edited.
4. `tdd_verify.py` — `Stop`. On session end, checks that the test suite is
   GREEN.

`test_tdd_hooks.py` covers this pipeline.

### 2. The STATUS board

A bundle that auto-refreshes a project's root `STATUS.md` every session:

- `scripts/status.py` — generates the issue table in `STATUS.md` from the
  issue files in `.scratch/`. **This is the canonical version.**
- The `status` skill — refreshes `STATUS.md` (`/status`). It calls `status.py`
  and updates the hand-written narrative sections.
- The `setup-status-harness` skill — installs the STATUS harness into a
  project that doesn't have one. It bundles its own copy of `status.py`, which
  `sync.sh` keeps in lockstep with the canonical one
  ([ADR-0001](docs/adr/0001-keel-is-a-sync-mirror.md)).
- The `SessionStart`/`Stop` hook wiring in `settings.json` runs `status.py`.

### 3. Dev logs

A bundle that, when a session ends, summarizes it in Korean and files it into
an Obsidian vault:

`session_devlog.py` (a `SessionEnd` hook) → spawns a detached headless
`claude` → the `korean-context-writer` agent + the `session-dev-log` skill →
Obsidian. `daily-dev-log` groups a whole day's sessions by project into the
same folder.

> ⚠️ This bundle has an Obsidian vault path hardcoded. See the Roadmap below.

### Standalone components

- `ai-readiness-cartography` — audits a repo against an AI-Ready rubric
  (100 points, 7 categories) and produces an HTML dashboard plus an
  ROI-ranked action list. Bundles `scripts/score.py`.
- `audit-and-write-readme` — deeply audits a project and writes a verified
  English/Korean README. Every claim has to pass a gate against the real
  files.
- `ci-setup` — reviews the current repo and generates GitHub Actions CI
  workflows per language. The `ci-researcher` sub-agent researches two
  toolchain options (modern / stable) on the web, and you decide through a
  five-gate flow.
- `daily-token-report` — aggregates a day's Claude Code token usage by
  project, model, session, and task into a self-contained HTML report, saved
  to the Obsidian Dev log folder.
- `tech-blog` — plain, honest Korean technical blog posts. Verified facts
  only, no exaggeration.
- `third-party-review` — deterministically reduces the current session
  transcript, feeds it to outside models (Codex, Gemini), and gets a
  third-party verdict on whether the human↔agent conversation has drifted
  enough to cause trouble later.
- `statusline.sh` — a custom status line.
- `sound_complete.sh` / `sound_permission.sh` — completion and
  permission-request notification sounds. **WSL only** (plays Windows sounds
  via `powershell.exe`).

---

## settings.json — harness vs. personal taste

`settings.json` mixes two things. When you borrow from it, you need to tell
them apart.

**Harness core** (needed for keel to work):

- `hooks` — all the hook wiring for the three bundles above.
- `statusLine` — wires up `statusline.sh`.

**Personal taste** (unrelated to the harness, no need to copy):

- `permissions` — personal deny rules.
- `effortLevel`, `autoCompactEnabled`, `remoteControlAtStartup`,
  `skipAutoPermissionPrompt`, `skipDangerousModePermissionPrompt`.
- `enabledPlugins`, `extraKnownMarketplaces` — plugin choices.

---

## Install

> There's no install script yet. keel starts small and adds one piece at a
> time.

By hand:

1. Copy the files under `skills/`, `hooks/`, `scripts/`, and `agents/` into the
   matching locations in your `~/.claude`.
2. Merge the **harness core** block of `settings.json` (`hooks`, `statusLine`)
   into your own `~/.claude/settings.json`.
3. The hook command paths are currently hardcoded to `/home/shine/...` — change
   them to your own home path.

---

## Maintenance — the working model

keel is a **downstream mirror** of `~/.claude`. `~/.claude` is the source of
truth; you don't edit keel directly
([ADR-0001](docs/adr/0001-keel-is-a-sync-mirror.md)).

```
edit in ~/.claude  →  ./sync.sh  →  commit / push in keel
```

`sync.sh` pulls only the authored content listed in [`.allowlist`](.allowlist)
from `~/.claude`, and excludes runtime cruft and third-party content.

---

## Roadmap

keel went public early — small, and growing one piece at a time. A few
hardening tasks are still open:

- [ ] Drop the hardcoded Obsidian paths — move the memory paths in
      `daily-dev-log`, `daily-token-report`, `session-dev-log`, `tech-blog`,
      and `korean-context-writer` into configuration.
- [ ] Genericize absolute paths — `/home/shine` → `$HOME`.
- [ ] Add a `LICENSE`.
- [ ] Split `settings.json` into a harness block and personal preference.
- [ ] Make `sound_*.sh` cross-platform (optional).

---

## Docs

- [`CONTEXT.md`](CONTEXT.md) — terminology.
- [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md) — third-party dependencies not
  vendored into keel.
- [`docs/adr/`](docs/adr/) — architecture decision records.
