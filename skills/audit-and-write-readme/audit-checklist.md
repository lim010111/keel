# Audit Checklist

Use this as the deep-scan reference. The goal is to extract every fact a README needs from disk before asking the user a single question.

## 1. Reconnaissance

Run `scripts/inventory.sh` first. It surfaces manifests, entry points, docs, CI, env-var hits, and git context. Read the surfaced files in full — do not skim.

## 2. Per-ecosystem reading

| Ecosystem | Read for | What you extract |
|-----------|----------|-------------------|
| **Node.js** | `package.json`, lockfile | `name`, `version`, `description`, `scripts`, `bin`, `main`, `engines`, public deps |
| **Python** | `pyproject.toml`, `setup.cfg`, `setup.py`, `requirements*.txt` | project name, version, console scripts, runtime constraints, deps |
| **Rust** | `Cargo.toml`, `Cargo.lock` | `[package]`, `[[bin]]`, feature flags, MSRV |
| **Go** | `go.mod`, `cmd/*/main.go` | module path, Go version, binaries |
| **Ruby** | `Gemfile`, `*.gemspec` | name, summary, executables, runtime deps |
| **JVM** | `pom.xml`, `build.gradle*` | groupId/artifactId, plugins, mainClass |

## 3. Operational signals

- `LICENSE` — extract the actual license name; do not infer from `package.json` alone
- `CONTRIBUTING.md` — exists? referenced from README?
- `.github/workflows/*` or `.gitlab-ci.yml` — CI badge candidates, test command
- `Dockerfile` / `docker-compose.yml` — runtime contract, exposed ports
- `.env.example` / `.envrc` — required env vars (cross-check with code)
- `Makefile` / `justfile` / `taskfile.yml` — canonical commands

## 4. Code shape

- Top-level directories: `src/`, `lib/`, `app/`, `cmd/`, `tests/`, `docs/`, `examples/`
- Public surface: exports in `index.*`, `__init__.py`, `lib.rs`, or `mod.rs`
- Test directory exists? Test framework detectable from manifest?

## 5. Things commonly missed

- **Hidden CLI** — `bin` field or `[project.scripts]` table; the package name and the binary name often differ
- **Required system tools** — calls to external CLIs (`git`, `ffmpeg`, `docker`) imply a prerequisite
- **Hidden env vars** — secrets referenced only in CI files, not in code
- **Monorepo structure** — `pnpm-workspace.yaml`, `lerna.json`, Cargo `[workspace]`, Nx — each subpackage may want its own README
- **Generated files** — anything under `dist/`, `build/`, `target/` is not source; do not document it
- **Platform-specific behavior** — `#[cfg(target_os = ...)]`, `sys.platform`, `process.platform` branches

## 6. Audit note

Keep a short in-conversation note (not a file) with:

```
Project: <name>
Type: <oss|internal|personal|config>
Language/stack: <...>
Entry point: <...>
Install: <command>
Run: <command>
Test: <command>
Env vars required: <...>
License: <...>
Notable highlights: <...>
Gaps to ask user: <...>
```

This note is the input to Step 4 (drafting).
