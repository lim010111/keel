---
name: audit-and-write-readme
description: Deeply audits a project (manifests, entry points, scripts, code, docs) and crafts a verified bilingual README — English primary plus Korean companion. Every claim passes a verification gate against real files before it ships. Use when the user wants to create or regenerate a README based on actual code, says "audit and README", "꼼꼼히 검토하고 README", "프로젝트 분석 후 README 작성", or asks to write a README from scratch for an existing project.
---

# Audit-and-Write README

Inspect the project deeply, verify every claim, then ship a bilingual README. Default output: `README.md` (English) and `README.ko.md` (Korean). Existing files are backed up before overwrite.

## Workflow

### 1. Audit — deep scan

Run `scripts/inventory.sh` from the project root for a fast snapshot, then read the files it surfaces. Cover all of:

- **Manifests** — `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `Gemfile`, `composer.json`, `pom.xml`, `build.gradle*`, `requirements*.txt`, `Pipfile*`, `setup.py`, `setup.cfg`
- **Entry points** — `main`/`bin`/`scripts` fields, `__main__.py`, `cmd/`, `src/index.*`, `cli.*`
- **Operations** — `README*`, `CHANGELOG*`, `LICENSE*`, `CONTRIBUTING*`, `docs/`, `.github/`, CI configs
- **Code shape** — top-level dirs, exported APIs, test directory
- **Runtime config** — env-var references (`process.env`, `os.environ`, `std::env::var`), `.env.example`
- **Git context** — recent commits, remote URL, author signal

See `audit-checklist.md` for the exhaustive list and per-language tips.

### 2. Classify project type

Decide from the audit; ask only if ambiguous.

| Type | Signal |
|------|--------|
| OSS | Public license, registry metadata (npm/PyPI/crates), CONTRIBUTING |
| Internal | Team/oncall references, internal hosts, no public license |
| Personal | Single-author git history, portfolio cues, no team context |
| Config | Dotfile-shaped, lives under `~/.config` or `dotfiles/`, no manifest |

Templates: `templates/{oss,personal,internal,xdg-config}.md`.

### 3. Targeted gap questions

Ask only what the audit could not extract. Always confirm:

1. One-line problem statement
2. Notable highlights or undocumented intent
3. Audience details the code can't reveal (e.g. who is `internal` *for*)

Skip questions whose answer is already on disk.

### 4. Draft against template

Pick the matching template, fill it from audit findings. Leave `[TODO: …]` placeholders only where information is genuinely missing.

### 5. Verification gates — mandatory

Before writing, every claim in the draft must pass at least one gate. See `verification-gates.md` for the grep recipes.

- [ ] **Install commands** match a real lockfile/manifest entry
- [ ] **Feature bullets** map to a symbol or directory that exists
- [ ] **Env vars** appear in source code
- [ ] **Scripts/commands** exist in `package.json` `scripts`, `Makefile`, `pyproject.toml`, etc.
- [ ] **License** statement matches the `LICENSE` file
- [ ] **Dependencies/versions** line up with the manifest

If a claim fails a gate: remove it, mark `[TODO: verify]`, or ask the user — never ship an unverified claim.

### 6. Generate bilingual output

- `README.md` — English primary
- `README.ko.md` — Korean companion, same sections in the same order
- Top of each file: a single language switch — e.g. `> [English](README.md) · [한국어](README.ko.md)`

Translate semantically, not literally. Code blocks, commands, file paths, identifiers stay identical across both files.

### 7. Backup and write

For each target file that already exists, rename to `README.md.bak` (or `README.ko.md.bak`). If a `.bak` already exists, suffix with the current date: `README.md.bak.YYYYMMDD`. Then write the new files.

## References

- `audit-checklist.md` — exhaustive inspection list, per-language tips
- `verification-gates.md` — gate rules with grep recipes
- `section-checklist.md` — which sections to include per project type
- `style-guide.md` — common README mistakes
- `templates/` — per-type templates (English; Korean is generated)
- `scripts/inventory.sh` — one-shot project scan
