---
name: setup-merge-gate
description: Install the merge-gate CI harness into a target project — a GitHub Actions workflow that runs Codex adversarial-review on every PR, runs the Claude validator to classify each finding as uphold/dismiss/unsure, and blocks merge on critical/high ∩ uphold. Writes `.github/workflows/codex-review.yml`, the `[merge-gate]` section of `harness.toml`, `docs/merge-gate.md`, `docs/merge-gate-operations.md`, and vendors the validator agent + runtime skill into the target's `.claude/`. Use when the user asks to "set up merge-gate", "install the merge gate", "wire up Codex CI review", "/setup-merge-gate", "머지 게이트 설치", "머지-게이트 깔아줘". Also supports `--uninstall`.
---

# Install the merge-gate harness into a project

The merge gate is the harness's pre-merge phase: every PR is run
through Codex's adversarial-review, classified by the Claude
validator, and either commented (soft mode) or blocked (hard mode).
This skill installs the gate's artefacts into a target project.

The MVP is **Claude-only** (ADR-0005). Gemini-based dual-validator
concordance is deferred until soft-mode measurement data is
available (issue #10).

## What gets installed

Under the target repo (defaults to `git rev-parse --show-toplevel`):

| Path | Source | Purpose |
|---|---|---|
| `.github/workflows/codex-review.yml`           | `templates/codex-review.yml` rendered | The CI workflow |
| `harness.toml` `[merge-gate]` section          | written from CLI flags                | Per-gate config (ADR-0003) |
| `docs/merge-gate.md`                            | `templates/merge-gate.md.template` rendered | Short reference for humans |
| `docs/merge-gate-operations.md`                 | `templates/OPERATIONS.md` (verbatim + marker) | Long-form operations playbook (#06) |
| `.claude/agents/codex-review-validator.md`     | vendored from `~/.claude/agents/`     | The validator agent definition (#02) |
| `.claude/skills/run-codex-validators/`         | vendored from `~/.claude/skills/`     | The runtime slash command (#05) |

The two vendored copies let clean CI runners discover the skill +
agent without a global `~/.claude/`. They are **snapshots** — re-running
this installer overwrites them unconditionally; projects that want
per-project tweaks should fork the upstream agent/skill instead.

## Prerequisites

- Inside a git repo. `git rev-parse --show-toplevel` must succeed.
- `AGENTS.md` exists at the repo root. If not, run `/setup-agents-md`
  first (issue #01) — the installer refuses to proceed without it
  because the validator reads project context from there.
- `python3` ≥ 3.11 on `$PATH` (uses `tomllib`).
- `~/.claude/agents/codex-review-validator.md` present (#02 — global agent definition).
- `~/.claude/skills/run-codex-validators/` present (#05 — runtime slash command).
  If #05 has not merged yet, pass `--validator-runtime-src
  <path-to-#05-worktree>` to `render.py` for local testing.

## Workflow

**1 — Pick the target.** Default = repo root from
`git rev-parse --show-toplevel`. Pass an explicit `--target <path>`
to install into a different working tree (e.g. a sibling worktree).

**2 — Collect placeholder values.** Read existing values if a
`[merge-gate]` section is already in `harness.toml`; otherwise use
sensible defaults:

| Token | Default |
|---|---|
| `project_name`         | repo basename |
| `soft_mode_default`    | `"true"` (gate ships in soft mode) |
| `docs_only_globs`      | `["**/*.md","docs/**","LICENSE","NOTICE"]` |
| `node_version`         | `"20"` |
| `codex_install_cmd`    | `npm install -g @openai/codex@latest` |
| `codex_review_cmd`     | `codex exec --json --output-schema .codex-review/schema.json --sandbox read-only "Run an adversarial review of the diff against origin/$BASE_REF"` |
| `bypass_label`         | `merge-gate-bypass` |

Use `AskUserQuestion` to confirm each value (or accept all defaults
in one go on a fresh install). For re-runs, show only the diff
between existing values and proposed values and ask once.

**Do not** invoke `input()` from `render.py` — Claude drives the
prompts so the user gets a structured UI; `render.py` only takes
CLI flags.

**3 — Dry run.** Always preview first:

```
python3 ~/.claude/skills/setup-merge-gate/scripts/render.py \
  --target <repo> --dry-run \
  --project-name <name> \
  --soft-mode-default <true|false> \
  --docs-only-globs '<json-array>' \
  --node-version <major> \
  --codex-install-cmd '<shell>' \
  --codex-review-cmd '<shell>' \
  --bypass-label <label>
```

Each line reports ✓ (already matches), `+` (will write or change),
or `⚠` (manual attention needed). Report this back to the user.

**4 — Apply.** Re-run the same command without `--dry-run`.

**5 — Print next steps.** `render.py` ends with a "Next steps"
block listing the Secrets to add to the GitHub repo
(`CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`) and the
branch-protection rule to add (`merge-gate / codex-review`).
Surface this to the user verbatim — these steps are HITL and the
skill cannot perform them.

## Uninstall

When the user asks to "remove merge-gate", "uninstall merge-gate",
"머지-게이트 제거", or runs `/setup-merge-gate --uninstall`:

```
python3 ~/.claude/skills/setup-merge-gate/scripts/render.py \
  --target <repo> --uninstall --dry-run
```

then re-run without `--dry-run`. Removes:

- `.github/workflows/codex-review.yml`
- the `[merge-gate]` section of `harness.toml` (other sections preserved — `tomllib` round-trip drift check)
- `.claude/agents/codex-review-validator.md`
- `.claude/skills/run-codex-validators/`
- `docs/merge-gate.md` **and** `docs/merge-gate-operations.md`, only if their first-line generated marker (`<!-- generated by /setup-merge-gate v1; do not edit by hand -->`) is intact. If the user has rewritten them, they are left in place with a `⚠`.

The skill **never** touches branch protection rules — those are
HITL. Tell the user to remove the branch-protection rule on
`merge-gate / codex-review` themselves.

## Idempotency

Re-running on a configured project:

- `harness.toml [merge-gate]` is a **Q&A diff** — values are loaded
  from the existing section as defaults, the user is shown the
  proposed delta, and the file is rewritten only if anything
  actually changes.
- Vendored agent / skill files are **overwritten unconditionally**
  (without prompting). They are snapshots, not source of truth —
  users who want project-specific tweaks should fork upstream.
- The rendered workflow, quick-ref doc, and operations doc are
  rewritten only if their content differs from what we would render
  now. Identical = ✓ no-op.

The drift check in `render.py` aborts if surgery on `harness.toml`
would change any non-`[merge-gate]` section. This is a safety net
against bugs in the section-replacement regex.

## Verification

After `--dry-run`, sanity-check:

- `grep -nE '%%[A-Z_]+%%' .github/workflows/codex-review.yml` — should
  be **empty** (all 7 tokens substituted).
- `python3 -c "import tomllib; print(sorted(tomllib.load(open('harness.toml','rb'))['merge-gate']))"`
  — should list all 7 lowercase placeholder keys.
- `head -1 docs/merge-gate.md` and `head -1 docs/merge-gate-operations.md`
  — both should start with `<!-- generated by /setup-merge-gate v1; do not edit by hand -->`.
- `grep -l "managed by /setup-merge-gate v1" .claude/agents/*.md
  .claude/skills/run-codex-validators/*.md` — should list every vendored file.

End-to-end CI verification (the workflow actually running and
gating a PR) requires #05 to be merged and `CLAUDE_CODE_OAUTH_TOKEN`
+ `OPENAI_API_KEY` to be wired up on the target GitHub repo. See
operations playbook §2 for the soft → hard promotion checklist.

## Notes

- The handful of placeholder values land in `harness.toml`'s
  `[merge-gate]` section (ADR-0003: one `harness.toml` per project,
  one `[<gate>]` table per gate). This is what makes re-running the
  installer a real diff, not a clobber.
- The workflow's stable identifiers are the workflow name
  `merge-gate` and the job id `codex-review`. Together they form
  the check `merge-gate / codex-review`, which is the branch-protection
  key. Do **not** rename either half across re-installs.
- Why the gate is Claude-only: see
  `docs/adr/0005-claude-only-validator-mvp-gemini-deferred.md` in
  the plan repo.
