# Dependencies

keel ships only authored content (see [ADR-0002](adr/0002-authored-content-only.md)).
The rest of the author's `~/.claude` setup is third-party. It is **not copied
into this repo** — it is listed here so the setup can be reproduced from its
upstream sources.

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
