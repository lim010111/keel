# Dependencies

keel ships only authored content (see [ADR-0002](adr/0002-authored-content-only.md)).
The rest of the author's `~/.claude` setup is third-party. It is **not copied
into this repo** — it is listed here so the setup can be reproduced from its
upstream sources.

## Runtime prerequisites

The harness shells out to these tools. Install the ones a component needs before
relying on it; if a tool is absent that component fails (mostly quietly) while
the rest of the harness keeps working.

| Tool | Needed by | If missing |
|---|---|---|
| `python3` **3.11+** | every hook + script (`status.py`, the `tdd_*` / `narrative_guard` / `grill_pause` hooks, `merge_gate_*`, `harness_doctor.py`) — the 3.11 floor is stdlib `tomllib` | those hooks no-op; the session still runs |
| `git` | `status.py`, `statusline.sh`, the merge-gate | per-repo features degrade |
| `jq` | `statusline.sh`, `sync.sh` settings redaction | status line drops the rate-limit blocks; `sync.sh` warns and skips redaction |
| `codex` CLI | the merge-gate `produce`, `consult-externals`, `third-party-review` | those reviews cannot run |
| `claude` CLI | `classify_sound.py` LLM fallback, the merge-gate validator dispatch | sound falls back to "complete"; the validator step is skipped |
| `wslpath` + `powershell.exe` (WSL) | `sound_*.sh` notification sounds, which play `$HOME/new_quest.mp3` / `$HOME/quest_completed.mp3` (the mp3s are personal, not shipped here) | `sound_*.sh` no-op cleanly on non-WSL |

## Skill-manager skills

Installed into `~/.agents/skills/` by a skill manager (lockfile:
`~/.agents/.skill-lock.json`) and symlinked into `~/.claude/skills/`.

| Source repo | Skills |
|---|---|
| `mattpocock/skills` | caveman †, codebase-design, diagnosing-bugs, domain-modeling, grill-me, grill-with-docs, grilling, handoff, improve-codebase-architecture, prototype, resolving-merge-conflicts, setup-matt-pocock-skills, tdd, to-issues, to-prd, triage, writing-great-skills, zoom-out † |
| `tdimino/claude-code-minoan` | agents-md-manager |
| `dceoy/speckit-agent-skills` | claude-command-converter |
| `vercel-labs/skills` | find-skills |
| `vercel-labs/agent-skills` | vercel-react-best-practices, web-design-guidelines |

> † `caveman`, `zoom-out` are pinned at their last upstream version — removed from
> `mattpocock/skills` with no successor (skill-suite-migration / ADR-0032). The gate
> is now the decomposed `grilling` + `domain-modeling` (+ `codebase-design`) core,
> invoked via the `grill-me` / `grill-with-docs` delegators.

## Plugins

Claude Code plugins (metadata in `~/.claude/plugins/installed_plugins.json`;
content re-downloaded on first run).

| Plugin | Marketplace | Enabled |
|---|---|---|
| `codex` | `openai/codex-plugin-cc` | yes |
| `claude-md-management` | `claude-plugins-official` | yes |
| `superpowers` | `claude-plugins-official` | no |
| `typescript-lsp` | `claude-plugins-official` | no |
| `humanize-korean` | `epoko77-ai/im-not-ai` | yes |

`humanize-korean` is a personal-taste plugin: `sync.sh` redacts it (and its
`im-not-ai` marketplace) from the mirrored `settings.json` (ADR-0002), so it is
recorded here but never published into this mirror.

## Merge-gate review assets

The local merge-gate producer (`merge_gate_local.py produce`) feeds Codex a
vendored adversarial-review prompt it reads from
`~/.claude/scripts/merge-gate-assets/adversarial-review.md`. That prompt is a
static copy of the Codex plugin's own prompt (`openai/codex-plugin-cc` →
`plugins/codex/prompts/adversarial-review.md`), so it is **third-party content
and is not mirrored into keel** ([ADR-0002](adr/0002-authored-content-only.md)),
like the plugins above.

Consequence on a fresh install: the merge-gate's fast `verify` and the
`setup-merge-gate` installer work without it, but the background `produce` finds
no prompt and **silently reviews nothing** — the adversarial pass never runs. To
enable it, copy that prompt from the installed Codex plugin into
`~/.claude/scripts/merge-gate-assets/`. The review-output schema the reviewer is
held to is already vendored (reused from
`skills/setup-merge-gate/templates/review-output.schema.json`).
