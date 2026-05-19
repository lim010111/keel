# Internal Project Template

For team codebases, services, and internal tools. Focus on onboarding new teammates and operational knowledge. Generate `README.md` (EN) and `README.ko.md` (KO) with the same skeleton.

---

# [Service / Project Name]

> [English](README.md) · [한국어](README.ko.md)

[One-line description of what this service does]

**Team**: [Team name / Slack channel]  
**On-call**: [Rotation or contact]  
**Last reviewed**: [YYYY-MM-DD]

## Overview

[2–3 sentences: what this does, why it exists, where it fits in the system.]

### Dependencies

- **Upstream**: [Services this depends on]
- **Downstream**: [Services that depend on this]

## Local development

### Prerequisites

- [Tool 1 with version, extracted from manifest or CI]
- [Tool 2]
- Access to [internal system / VPN / secret store]

### Environment variables

| Variable | Description | Where to get it |
|----------|-------------|-----------------|
| `[VAR]` | [Description, verified by grep in source] | [Vault / 1Password / etc] |

### Running locally

```bash
[Verified commands from Makefile / scripts]
```

### Running tests

```bash
[Test command from manifest]
```

## Architecture

[Brief design summary. Link to architecture doc if it exists.]

```
[Optional ASCII diagram]
```

### Key files

| Path | Purpose |
|------|---------|
| `[path]` | [Verified against actual file] |

## Deployment

[How to deploy, or link to deployment docs.]

### Environments

| Environment | URL | Notes |
|-------------|-----|-------|
| Dev | [URL] | [Notes] |
| Staging | [URL] | [Notes] |
| Prod | [URL] | [Notes] |

## Runbooks

### [Common task]

```bash
[Steps]
```

## Troubleshooting

### [Common problem]

**Symptom**: [What you see]  
**Cause**: [Why]  
**Fix**: [Resolution]

## Contributing

[Link to team's PR / review process.]

## Related docs

- [Design doc]
- [API docs]
- [Dashboard]
