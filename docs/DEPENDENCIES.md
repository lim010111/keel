# Dependencies

keel ships only authored content (see [ADR-0002](adr/0002-authored-content-only.md)).
The rest of the author's `~/.claude` setup is third-party. It is **not copied
into this repo** — it is listed here so the setup can be reproduced from its
upstream sources.

## gstack

A submodule in `~/.claude/skills/gstack` providing ~46 skills (`ship`,
`autoplan`, `review`, `qa`, `land-and-deploy`, the `plan-*` and `design-*`
families, …).

- Source: <https://github.com/garrytan/gstack.git>
- Installed as a git submodule at `~/.claude/skills/gstack`.

## Skill-manager skills

Installed into `~/.agents/skills/` by a skill manager (lockfile:
`~/.agents/.skill-lock.json`) and symlinked into `~/.claude/skills/`.

| Source repo | Skills |
|---|---|
| `mattpocock/skills` | caveman, diagnose, grill-me, grill-with-docs, handoff, improve-codebase-architecture, prototype, setup-matt-pocock-skills, tdd, to-issues, to-prd, triage, write-a-skill, zoom-out |
| `tdimino/claude-code-minoan` | agents-md-manager |
| `dceoy/speckit-agent-skills` | claude-command-converter |
| `vercel-labs/skills` | find-skills |
| `vercel-labs/agent-skills` | vercel-react-best-practices, web-design-guidelines |

## Plugins

Claude Code plugins (metadata in `~/.claude/plugins/installed_plugins.json`;
content re-downloaded on first run).

| Plugin | Marketplace | Enabled |
|---|---|---|
| `codex` | `openai/codex-plugin-cc` | yes |
| `claude-md-management` | `claude-plugins-official` | yes |
| `superpowers` | `claude-plugins-official` | no |
| `typescript-lsp` | `claude-plugins-official` | no |
