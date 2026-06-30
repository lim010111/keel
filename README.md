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
That is the harness *within* a unit of work — for how it gates the **trust
transitions** an artifact crosses on its way to a wider audience, see
[`docs/HARNESS-DESIGN.md`](docs/HARNESS-DESIGN.md).

---

## What's inside

| Kind | Items |
|---|---|
| Skills | `ai-readiness-cartography`, `audit-and-write-readme`, `ci-setup`, `consult-externals`, `daily-dev-log`, `daily-token-report`, `handle-merge-findings`, `harden-issue`, `harness-doctor`, `improve-prompt`, `run-codex-validators`, `session-dev-log`, `setup-agents-md`, `setup-merge-gate`, `setup-status-harness`, `status`, `tech-blog`, `third-party-review` |
| Hooks | `tdd_keyword` · `tdd_guard` · `tdd_mark` · `tdd_verify`, `narrative_guard` · `grill_pause`, `merge_gate_post_commit` · `merge_gate_scheduler` |
| Scripts | `status.py`, `sound_complete.sh` · `sound_permission.sh` · `classify_sound.py`, `merge_gate_local.py` · `merge_gate_adjudicate.py` · `merge_gate_measure.py`, `harness_doctor.py`, `toml_sections.py`, `check_alignment_skill_drift.py` |
| Agents | `ci-researcher`, `codex-review-validator` |
| Config | `CLAUDE.md`, `statusline.sh`, `settings.json` |

These components aren't all independent. The four bundles below **only work
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
  issue files in `.scratch/`. **This is the canonical version.** Its `--brief`
  mode prints a session-start view (narrative + actionable rows only;
  done/parked rows collapse to counts) so the standing per-session context
  cost stays small. `test_status.py` covers it.
- The `status` skill — refreshes `STATUS.md` (`/status`). It calls `status.py`
  and updates the hand-written narrative sections.
- The `setup-status-harness` skill — installs the STATUS harness into a
  project that doesn't have one. It bundles its own copy of `status.py`, which
  `sync.sh` keeps in lockstep with the canonical one
  ([ADR-0001](docs/adr/0001-keel-is-a-sync-mirror.md)).
- The `SessionStart`/`Stop` hook wiring in `settings.json` runs `status.py`
  (`--brief` at session start).
- `narrative_guard.py` — the enforcement half for the hand-written narrative
  block. Takes a `SessionStart` snapshot, then a blocking `Stop` check refuses
  to end a turn when the project's posture changed this session but the
  narrative was left byte-unchanged, re-prompting the agent to run `/status`.
  It also blocks completion-labelled track lines written into the narrative
  (the board's labels are a closed set; finished tracks are deleted, not
  relabelled). `test_narrative_guard.py` covers it.
- `grill_pause.py` — a `PreToolUse(Skill)` hook that pauses `narrative_guard` for
  the duration of a grilling session (`grill-me` / `grill-with-docs` /
  `harden-issue`), so the inline ADR/issue edits don't trip the `Stop` check
  mid-session; `/status` re-arms it. The pause lives in this owned hook rather
  than in the rented grilling skills' prose. `test_grill_pause.py` covers it.
- `check_alignment_skill_drift.py` — a `SessionStart` advisory that recomputes
  the rented alignment skills' git-tree hash and warns (only on drift) if an
  upstream `skills` update changed them underfoot. `test_check_alignment_skill_drift.py`
  covers it.

### 3. Dev logs

A bundle that summarizes a session in Korean and files it into an Obsidian
vault:

The `session-dev-log` skill (invoked as `/session-dev-log`) → Obsidian.
`daily-dev-log` groups a whole day's sessions by project into the same
folder. (The `SessionEnd` hook that auto-invoked this on every session
end has been retired — invocation is manual now.)

> ⚠️ This bundle has an Obsidian vault path hardcoded. See the Roadmap below.

### 4. The merge gate

A bundle that installs and runs a pre-merge gate in a target project: a
reviewer set (Codex by default) does the adversarial review, then a Claude
validator subagent classifies each finding (`uphold`/`dismiss`/`unsure`)
against the project's own documented context, and the gate blocks merge on
`critical`/`high` ∩ `uphold` (with `unsure` failing safe behind a labelled
bypass lane). MVP is Claude-only.

The gate is **local-only**: a per-repo `pre-push` hook runs
`merge-gate-local verify` — fast, deterministic, and spends no Codex or
Claude tokens — while a `post-commit` hook launches the expensive `produce`
in the background, so there is no per-PR CI cost. (A github-actions profile
existed and was removed outright.)

- `setup-merge-gate` — the installer: a `pre-push` hook + a `[merge-gate]`
  section in the project's `harness.toml`. `--uninstall` reverses everything,
  leaving docs alone if the user has edited their generated marker out.
- `run-codex-validators` — the validator runtime, invoked by the local
  producer (`merge_gate_local.py produce`) or a human after a local
  `codex /adversarial-review`. Reads Codex JSON, adapts it to the validator
  agent's input shape, dispatches the subagent via the Agent tool, aggregates
  verdicts, and writes `validators.{json,md}` for the merge gate to consume.
  **Always exits 0**; the merge-gate's `verify` step is the sole
  authoritative gate.
- `codex-review-validator` (agent) — the classifier itself. Takes one
  finding at a time and returns `uphold`/`dismiss`/`unsure` plus a strict
  citation rule: a `dismiss` without a quoted code/doc line auto-promotes
  to `unsure`.
- `handle-merge-findings` — the consumer-side loop for the gate's **advisory**
  findings. After a push it reads them (via `merge_gate_local.py findings`),
  then runs one human-gated pass that **reproduces or refutes** each finding
  with a runnable failing test before fixing only the proven ones — the
  validator verdict is a hint, never a filter, and the gate itself is never
  touched (the loop only reads).
- `merge_gate_local.py` — the local-profile wrapper, run outside any Claude
  session by the `pre-push` hook. `produce` (expensive) runs the reviewer set
  + the Claude validator and writes a cached artefact; `verify` (fast) only
  reads that artefact's summary and exits 0 (pass) or 1 (block) — no Codex, no
  Claude, no writes; `findings` reads the cached artefact back (read-only) for
  the consumer loop; `force` re-produces ignoring the cache.
- `merge_gate_post_commit.py` — the trigger behind the background `produce`:
  a git `post-commit` hook that, when the just-made commit touched in-scope
  files, launches a backgrounded, commit-pinned `produce` for the committed
  tip. Repo-scoped where its predecessors (`merge_gate_mark.py` dirty marker
  + `merge_gate_scheduler.py` Stop scheduler) were session-cwd-scoped and
  silently never fired in a two-repo workflow. Only the scheduler's Stop
  *registration* was retired — `merge_gate_scheduler.py` itself stays a live
  runtime dependency: the shared state library (`repo_state_dir` /
  `load_state` / `save_state`) that `merge_gate_post_commit.py` imports, and
  part of `merge_gate_local.py`'s pinned runtime set. `test_merge_gate_post_commit.py`,
  `test_merge_gate_hooks.py`, and `test_merge_gate_local.py` cover the bundle.
- `merge_gate_measure.py` / `merge_gate_adjudicate.py` — the measurement
  layer for the soft-mode window: `measure` instruments around the gate
  (never changing a decision) and appends freshness/verdict/latency signals
  to a per-repo ledger; `adjudicate` is the blind verdict-capture UX that
  fills the ledger's human columns — it shows each finding *before* revealing
  the gate's mechanical verdict, so false positives are detectable.

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
- `consult-externals` — mid-grilling council. Pulls two outside models
  (Codex, agy) into a single decision point in parallel, each producing a
  structured five-section report grounded in the repo and a slice of the
  live conversation. The skill never picks a winner — disagreement between
  the two is the load-bearing signal.
- `daily-token-report` — aggregates a day's Claude Code token usage by
  project, model, session, and task into a self-contained HTML report, saved
  to the Obsidian Dev log folder.
- `harness-doctor` — diagnoses a target repo's harness-scaffold conformance:
  which applicable gates/skills are installed vs missing, on the intent and
  enforcement axes, then fills the gaps by delegating to each setup skill's
  own apply path (consent-gated). `scripts/harness_doctor.py` is the
  read-only diagnosis engine; `scripts/toml_sections.py` is the shared
  section-scoped TOML text library the house installers use to edit
  `harness.toml` without disturbing sibling sections.
- `setup-agents-md` — bootstraps the `AGENTS.md` ↔ `CLAUDE.md` relationship
  in a target repo. `AGENTS.md` is the canonical agent guidance read
  directly by Codex / antigravity; `CLAUDE.md` `@import`s it so Claude Code
  sees the same content. Also handles nested `AGENTS.md` under module
  subdirectories.
- `tech-blog` — plain, honest Korean technical blog posts. Verified facts
  only, no exaggeration.
- `third-party-review` — deterministically reduces the current session
  transcript, feeds it to outside models (Codex, Gemini), and gets a
  third-party verdict on whether the human↔agent conversation has drifted
  enough to cause trouble later.
- `statusline.sh` — a custom status line.
- `sound_complete.sh` / `sound_permission.sh` — completion and
  permission-request notification sounds. **WSL only** (plays Windows sounds
  via `powershell.exe`). `classify_sound.py` decides *which* sound: it
  classifies whether the final assistant turn was a question to the user or
  a plain completion (heuristics + Haiku fallback), since the API exposes no
  metadata distinguishing the two.

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
3. The hook command paths use `$HOME` (expanded by the shell at run time), so
   they resolve correctly once the files live under your own `~/.claude` — no
   per-path editing needed.

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
      `daily-dev-log`, `daily-token-report`, `session-dev-log`, and
      `tech-blog` into configuration.
- [x] Genericize the `settings.json` hook command paths — `/home/shine` → `$HOME`.
- [ ] Genericize the remaining hardcoded absolute paths (e.g. the merge-gate
      ledger default in `merge_gate_adjudicate.py`) — the deferred
      arbitrary-home-layout work.
- [ ] Split `settings.json` into a harness block and personal preference.
- [ ] Make `sound_*.sh` cross-platform (optional).

---

## Docs

- [`CONTEXT.md`](CONTEXT.md) — terminology.
- [`docs/HARNESS-DESIGN.md`](docs/HARNESS-DESIGN.md) — how the harness gates
  trust transitions (the four-gate v1 shape).
- [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md) — third-party dependencies not
  vendored into keel.
- [`docs/adr/`](docs/adr/) — architecture decision records.
