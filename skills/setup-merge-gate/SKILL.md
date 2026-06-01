---
name: setup-merge-gate
description: Install the merge-gate harness into a target project. Defaults to the LOCAL profile (claude-harness-work ADR-0009) — a pre-push hook that runs `merge-gate-local verify`, backed by a local `produce` that runs Codex adversarial-review + the Claude validator with no per-PR CI spend. The github-actions profile (a PR-blocking GitHub Actions workflow) is dormant/opt-in, reached only via `--profile github-actions`. Use when the user asks to "set up merge-gate", "install the merge gate", "wire up Codex review", "/setup-merge-gate", "머지 게이트 설치", "머지-게이트 깔아줘". Also supports `--uninstall`.
---

# Install the merge-gate harness into a project

The merge gate runs Codex adversarial-review, classifies each finding with the
Claude validator (uphold/dismiss/unsure — ADR-0005, Claude-only MVP), and
blocks on validator-upheld/unsure critical/high findings. It installs under one
of two **Gate profiles** (CONTEXT.md → *Gate profile*):

| Profile | Venue | Default | Spend | Status |
|---|---|---|---|---|
| **local** (default) | local `codex` + headless `claude`, pre-push enforcement | advisory | none | active (ADR-0009) |
| **github-actions** | GitHub Actions on every PR, branch-protection enforcement | dormant | per-PR API | **frozen / opt-in only** (ADR-0009) |

**Pick the profile first.** No `--profile` flag (or `--profile local`) → the
**local** profile below. `--profile github-actions` → the dormant GHA profile
(§ "GitHub Actions profile"), which is **frozen**: do not enable it without an
explicit operator request.

---

## Local profile (default)

The local profile installs:

| What | Where | Purpose |
|---|---|---|
| pre-push hook | `<repo>/.git/hooks/pre-push` | calls `merge-gate-local verify` only |
| `[merge-gate]` + `[merge-gate.local*]` | `<repo>/harness.toml` | profile + local config (D8) |
| `.codex-review/` ignore | `<repo>/.gitignore` | the artefact cache is not committed (D3) |
| global Stop + PostToolUse hooks | `~/.claude/settings.json` | the cheap auto-`produce` scheduler (D2) |

The wrapper itself (`~/.claude/scripts/merge_gate_local.py`) and its hooks ship
globally with this repo — the installer only wires the per-repo pieces and
registers the global hooks if absent.

### Workflow

**1 — Pick the target.** Default = `git rev-parse --show-toplevel`. The repo
must be a git repo with `AGENTS.md` at the root (run `/setup-agents-md` first
if missing — the validator reads project context from there).

**2 — Detect an existing GHA install.** If `.github/workflows/codex-review.yml`
exists, the repo is currently on (or was set up for) the github-actions
profile. Switching it to local is the single **operator-authorized freeze
exception** (ADR-0009 § freeze *Exception*, 2026-05-28): you MAY tear down the
installed workflow, but **only after a HITL confirmation**. Ask the user
explicitly ("Found an installed GHA merge-gate workflow. Remove it and switch
this repo to the zero-CI-spend local profile?") and pass `--teardown-gha` only
on a yes. Never tear down GHA branch protection / secrets — that stays HITL.

**3 — Install.** Run the helper (deterministic; safe to re-run — idempotent):

```
python3 ~/.claude/skills/setup-merge-gate/scripts/install_local.py \
  --repo <repo> [--teardown-gha]
```

It writes the `harness.toml` local sections (preserving any
`[merge-gate.github-actions]` block and unrelated tables verbatim), installs
the pre-push hook, adds the `.gitignore` entry, and registers the global
`merge_gate_mark.py` (PostToolUse Edit|Write) + `merge_gate_scheduler.py` (Stop)
hooks in `~/.claude/settings.json` if they are not already there. It prints a
JSON summary; surface it.

**4 — Seed the first review and print next steps.** The pre-push gate is
**advisory** by default — it reports but never blocks (ADR-0009: what makes it a
gate is the independent, recorded, freshness-covering verdict, not blocking).
Tell the user:

- Run `python3 ~/.claude/scripts/merge_gate_local.py produce` once now to seed a
  review of the current changes (or just keep working — the Stop scheduler
  auto-produces when `auto_produce = "stop-debounced"`).
- On `git push`, the pre-push hook runs `verify`: advisory prints the verdict
  and lets the push through; to **promote** to blocking, set
  `[merge-gate.local].enforcement_policy = "client-side-blocking"` in
  `harness.toml` (run a measurement window first — issue #31).
- Audited bypass under blocking: add a `Merge-Gate-Bypass: <reason>` trailer to
  the tip commit. Unaudited escape hatch: `git push --no-verify`.

### Uninstall (local)

When asked to remove the local gate: delete `<repo>/.git/hooks/pre-push` (if it
is the merge-gate one — it contains `merge-gate-local verify`), remove the
`[merge-gate.local*]` sections from `harness.toml`, and optionally remove the
`.gitignore` entry. The global hooks self-gate (no-op outside local-profile
repos), so they can stay registered; remove them from `~/.claude/settings.json`
only if the user wants the machine fully clean.

---

## GitHub Actions profile (`--profile github-actions`) — frozen / opt-in

> **Frozen (ADR-0009).** The server-side profile is not under active
> consideration. Do **not** install, modify, re-vendor, or enable it unless the
> operator explicitly opts in (e.g. a repo starts taking community PRs). The
> instructions below are preserved for that opt-in case and are otherwise
> dormant. `#25` (CODEX_API_KEY trust boundary) must be resolved before any
> repo re-enables it.

The github-actions profile installs a CI workflow that runs Codex on every PR
and gates merge via branch protection.

### What gets installed

| Path | Source | Purpose |
|---|---|---|
| `.github/workflows/codex-review.yml`           | `templates/codex-review.yml` rendered | The CI workflow |
| `harness.toml` `[merge-gate]` section          | written from CLI flags                | Per-gate config (ADR-0003) |
| `docs/merge-gate.md`                            | `templates/merge-gate.md.template` rendered | Short reference for humans |
| `docs/merge-gate-operations.md`                 | `templates/OPERATIONS.md` (verbatim + marker) | Long-form operations playbook (#06) |
| `.claude/agents/codex-review-validator.md`     | vendored from `~/.claude/agents/`     | The validator agent definition (#02) |
| `.claude/skills/run-codex-validators/`         | vendored from `~/.claude/skills/`     | The runtime slash command (#05) |

The two vendored copies let clean CI runners discover the skill + agent without
a global `~/.claude/`. They are **snapshots** — re-running this installer
overwrites them unconditionally.

### Prerequisites

- Inside a git repo. `git rev-parse --show-toplevel` must succeed.
- `AGENTS.md` exists at the repo root (else `/setup-agents-md` first — #01).
- `python3` ≥ 3.11 on `$PATH` (uses `tomllib`).
- `~/.claude/agents/codex-review-validator.md` present (#02).
- `~/.claude/skills/run-codex-validators/` present (#05).

### Workflow

**1 — Pick the target.** Default = repo root. `--target <path>` for a sibling worktree.

**2 — Collect placeholder values.** Read existing values if a `[merge-gate]`
section is already in `harness.toml`; otherwise use sensible defaults:

| Token | Default |
|---|---|
| `project_name`         | repo basename |
| `soft_mode_default`    | `"true"` (gate ships in soft mode) |
| `docs_only_globs`      | `["**/*.md","docs/**","LICENSE","NOTICE"]` |
| `trust_doc_globs`      | `["AGENTS.md","**/AGENTS.md","CLAUDE.md","**/CLAUDE.md","CONTEXT-MAP.md","**/CONTEXT.md","docs/adr/**"]` |
| `node_version`         | `"20"` |
| `codex_install_cmd`    | `npm install -g @openai/codex@latest` |
| `codex_review_cmd`     | `codex exec --json --output-schema .codex-review/schema.json --dangerously-bypass-approvals-and-sandbox "Run an adversarial review of the diff against origin/$BASE_REF"` |
| `bypass_label`         | `merge-gate-bypass` |

Use `AskUserQuestion` to confirm each value. For re-runs, show only the diff
between existing and proposed values. A project installed before
`trust_doc_globs` existed will be missing that key — treat it as absent and
offer the default above (claude-harness-work#27).

**Do not** invoke `input()` from `render.py` — Claude drives the prompts;
`render.py` only takes CLI flags.

**3 — Dry run.** Always preview first:

```
python3 ~/.claude/skills/setup-merge-gate/scripts/render.py \
  --target <repo> --dry-run \
  --project-name <name> \
  --soft-mode-default <true|false> \
  --docs-only-globs '<json-array>' \
  --trust-doc-globs '<json-array>' \
  --node-version <major> \
  --codex-install-cmd '<shell>' \
  --codex-review-cmd '<shell>' \
  --bypass-label <label>
```

Each line reports ✓ (already matches), `+` (will write or change), or `⚠`
(manual attention needed). Report this back to the user.

**4 — Apply.** Re-run the same command without `--dry-run`.

**5 — Print next steps.** `render.py` ends with a "Next steps" block listing the
Secrets to add (`CLAUDE_CODE_OAUTH_TOKEN`, `CODEX_API_KEY`) and the
branch-protection rule (`merge-gate / codex-review`). Surface verbatim — these
are HITL.

### Uninstall (github-actions)

`/setup-merge-gate --uninstall --profile github-actions`:

```
python3 ~/.claude/skills/setup-merge-gate/scripts/render.py \
  --target <repo> --uninstall --dry-run
```

then re-run without `--dry-run`. Removes the workflow, the `[merge-gate]`
section of `harness.toml` (other sections preserved), the vendored agent +
skill, and `docs/merge-gate*.md` only if their generated marker is intact. Never
touches branch protection — tell the user to remove the
`merge-gate / codex-review` rule themselves.

### Idempotency, verification

`harness.toml [merge-gate]` is a Q&A diff; vendored files are overwritten
unconditionally; the rendered workflow/docs are rewritten only on content
change. `render.py`'s drift check aborts if surgery on `harness.toml` would
touch any non-`[merge-gate]` section. After a dry run, sanity-check:

- `grep -nE '%%[A-Z_]+%%' .github/workflows/codex-review.yml` — empty.
- `head -1 docs/merge-gate.md` / `docs/merge-gate-operations.md` — start with the generated marker.

The workflow's stable identifiers are the workflow name `merge-gate` and job id
`codex-review` (the branch-protection key). Do not rename either across re-installs.

---

## Notes

- Why the gate is Claude-only: `docs/adr/0005-claude-only-validator-mvp-gemini-deferred.md`.
- Local-first posture and the freeze: `docs/adr/0009-merge-gate-local-first-posture.md`.
- Reviewer set / composition / Codex command: ADR-0010/0011/0012; local-profile
  implementation: issue `#30`.
