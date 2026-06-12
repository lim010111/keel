---
name: setup-merge-gate
description: Install the merge-gate harness into a target project — the LOCAL profile (the only profile; ADR-0009 + ADR-0021): a pre-push hook that runs `merge-gate-local verify`, backed by a local `produce` that runs a reviewer set (Codex by default) adversarial-review + the Claude validator with no per-PR CI spend. Use when the user asks to "set up merge-gate", "install the merge gate", "wire up Codex review", "/setup-merge-gate", "머지 게이트 설치", "머지-게이트 깔아줘". Also supports `--uninstall`.
---

# Install the merge-gate harness into a project

The merge gate runs an adversarial review — by default Codex, though the producer
is a combinable **reviewer set** (Codex, Claude, AGY — ADR-0010) — classifies each
finding with the Claude validator (uphold/dismiss/unsure — ADR-0005, Claude-only
MVP), and blocks on validator-upheld/unsure critical/high findings. It installs
the **local profile** — the only profile (CONTEXT.md → *Gate profile*; the
github-actions profile was removed, ADR-0021).

The local profile installs:

| What | Where | Purpose |
|---|---|---|
| pre-push hook | `<repo>/.git/hooks/pre-push` | calls `merge-gate-local verify` only |
| `[merge-gate]` + `[merge-gate.local*]` | `<repo>/harness.toml` | profile + local config (D8) |
| `.merge-gate/` ignore | `<repo>/.gitignore` | the artefact cache is not committed (D3) |
| global Stop + PostToolUse hooks | `~/.claude/settings.json` | the cheap auto-`produce` scheduler (D2) |

The wrapper itself (`~/.claude/scripts/merge_gate_local.py`) and its hooks ship
globally with this repo — the installer only wires the per-repo pieces and
registers the global hooks if absent.

### Workflow

**1 — Pick the target.** Default = `git rev-parse --show-toplevel`. The repo
must be a git repo with `AGENTS.md` at the root (run `/setup-agents-md` first
if missing — the validator reads project context from there).

**2 — Install.** Run the helper (deterministic; safe to re-run — idempotent):

```
python3 ~/.claude/skills/setup-merge-gate/scripts/install_local.py \
  --repo <repo>
```

It writes the `harness.toml` local sections (preserving unrelated tables
verbatim), installs the pre-push hook **and the `post-commit` auto-produce
trigger** (#33/ADR-0014; both mirror the marker + `.pre-merge-gate`
foreign-backup convention), adds the `.gitignore` entry, and **cleans any
stale** global `merge_gate_mark.py` (PostToolUse) / `merge_gate_scheduler.py`
(Stop) registrations from `~/.claude/settings.json` — those hooks are
**retired** (the repo-scoped `post-commit` hook replaces them; the old Stop
trigger self-gated on the session cwd and never fired under the two-repo
workflow). It no longer registers any global hooks. It prints a JSON summary;
surface it.

**3 — Seed the first review and print next steps.** The pre-push gate is
**advisory** by default — it reports but never blocks (ADR-0009: what makes it a
gate is the independent, recorded, freshness-covering verdict, not blocking).
Tell the user:

- Run `python3 ~/.claude/scripts/merge_gate_local.py produce` once now to seed a
  review of the current changes (or just keep working — the `post-commit` hook
  auto-produces a commit-pinned review of HEAD after each in-scope commit, and
  the next push waits for it; #33/ADR-0014).
- On `git push`, the pre-push hook runs `verify`: advisory prints the verdict
  and lets the push through; to **promote** to blocking, set
  `[merge-gate.local].enforcement_policy = "client-side-blocking"` in
  `harness.toml` (run a measurement window first — issue #31).
- Audited bypass under blocking: add a `Merge-Gate-Bypass: <reason>` trailer to
  the tip commit. Unaudited escape hatch: `git push --no-verify`.

### Uninstall

Run the helper's uninstall path — it removes our `pre-push` and `post-commit`
hooks (restoring a foreign `*.pre-merge-gate` backup when present) and
deregisters any stale Stop/PostToolUse registrations:

```
python3 ~/.claude/skills/setup-merge-gate/scripts/install_local.py \
  --repo <repo> --uninstall
```

It leaves `harness.toml`'s `[merge-gate.local*]` sections and the `.gitignore`
entry in place — remove those by hand if you want the repo fully clean.

---

## Notes

- Why the gate is Claude-only: `docs/adr/0005-claude-only-validator-mvp-gemini-deferred.md`.
- Local-first posture: `docs/adr/0009-merge-gate-local-first-posture.md`; the
  github-actions profile it kept dormant was later **removed** —
  `docs/adr/0021-github-actions-merge-gate-profile-removed.md`.
- Reviewer set / composition / Codex command: ADR-0010/0011/0012; local-profile
  implementation: issue `#30`.
- `templates/review-output.schema.json` is **live local runtime** (the schema
  the producer holds Codex to — ADR-0012; see
  `scripts/merge-gate-assets/PROVENANCE.md`), not an installer template.
