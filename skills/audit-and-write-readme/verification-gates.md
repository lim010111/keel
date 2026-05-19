# Verification Gates

Every claim in the draft README must pass at least one gate before the file is written. A gate is a deterministic check against the actual repo. If a check fails, the claim is removed, marked `[TODO: verify]`, or escalated to the user.

## Gate 1 — Install commands

Claim form: "Install with `<command>`".

Pass if:

- The command names the package found in the manifest, **or**
- The command (e.g. `pip install -e .`, `cargo build`, `go install ./...`) is appropriate for the detected ecosystem **and** the manifest is present.

Checks:

```bash
# Node
jq -r '.name' package.json
# Python
grep -E '^name\s*=' pyproject.toml
# Rust
grep -E '^name\s*=' Cargo.toml
```

## Gate 2 — Feature bullets

Claim form: "Supports <X>".

Pass if a grep for the feature's identifier(s) returns a hit in source (not docs, not tests). Reject vague marketing language with no code anchor.

```bash
grep -RIn --include='*.{py,ts,tsx,js,jsx,rs,go,rb,java,kt}' '<symbol or keyword>' src/ lib/ app/ 2>/dev/null
```

## Gate 3 — Environment variables

Claim form: "Set `FOO_BAR` to …".

Pass if the variable name appears in source code (not only in `.env.example` or docs).

```bash
grep -RIn -E "process\.env\.FOO_BAR|os\.environ(\.get)?\(['\"]FOO_BAR|std::env::var\(['\"]FOO_BAR" . 2>/dev/null
```

If the variable lives only in CI or `.env.example`, mark it `[TODO: confirm runtime usage]`.

## Gate 4 — Scripts and commands

Claim form: "Run `npm run build`" or "`make test`".

Pass if the script exists in:

- `package.json` → `.scripts.<name>`
- `Makefile` / `justfile` → a target line
- `pyproject.toml` → `[tool.poetry.scripts]`, `[project.scripts]`, or a `[tool.*]` table
- `taskfile.yml` → a task entry

```bash
jq -r '.scripts // {} | keys[]' package.json 2>/dev/null
grep -E '^[a-zA-Z_-]+:' Makefile 2>/dev/null
```

## Gate 5 — License

Claim form: "Licensed under <X>".

Pass if the `LICENSE` file's first non-blank line names the same license. SPDX identifiers in the manifest are a secondary signal, not authoritative.

```bash
head -5 LICENSE 2>/dev/null
```

If `LICENSE` is missing but the manifest declares one, mark `[TODO: add LICENSE file]` rather than silently asserting it.

## Gate 6 — Dependencies and versions

Claim form: "Requires Node 18+" or "Built on Foo 4.x".

Pass if the constraint appears in the manifest (`engines`, `python_requires`, `rust-version`, `go` directive, etc.). Do not infer "modern" or "latest" without a constraint to point at.

## What to do on failure

| Failure | Action |
|---------|--------|
| Claim is wrong | Remove from draft |
| Claim is plausible but unverifiable | Replace with `[TODO: verify]` |
| Claim is load-bearing for the README | Ask the user one targeted question |

Do not write a README with unverified claims left in place — the gate exists because a wrong install command or a missing env var costs more than a missing section.
