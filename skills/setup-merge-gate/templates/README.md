# `setup-merge-gate` templates

Files in this directory are rendered into the target project by the
`/setup-merge-gate` **Setup skill** (issue #04). They are templates,
not live config — edit them here to change what every future install
ships, and run the install skill on a project to apply changes
there.

| Template | Renders into target project as | Source issue |
|---|---|---|
| `OPERATIONS.md` | `docs/merge-gate-operations.md` | #06 |
| `codex-review.yml` | `.github/workflows/codex-review.yml` | #03 |
| `merge-gate.md.template` | `docs/merge-gate.md` | #04 |

The skill itself lives at `~/.claude/skills/setup-merge-gate/SKILL.md`
(see issue #04).

Tokens of the form `%%NAME%%` are placeholders the installer must
substitute verbatim **before** the file is committed into the target
repo. Other expressions of the form `${{ … }}` are real GitHub Actions
expressions — leave them alone.

## Stable identifiers — do not rename in renders

- Workflow name: `merge-gate`
- Main job id:   `codex-review`

Branch-protection rules reference the check `merge-gate / codex-review`
(workflow name + " / " + job name). Renaming either half breaks every
protection rule that points at this gate.

## Placeholder tokens

`/setup-merge-gate` MUST replace each token with a project-appropriate value
before writing the file. None of the tokens may remain in the rendered file.

| Token | Type | Example | Notes |
|---|---|---|---|
| `%%PROJECT_NAME%%` | string | `keel` | Free-form human label; surfaced in the sticky comment. |
| `%%SOFT_MODE_DEFAULT%%` | `"true"` \| `"false"` | `"true"` | Default posture. Soft phase: `"true"` (findings reported, never block). Hard phase: `"false"` (critical/high block). Must be the string `true` or `false`. |
| `%%DOCS_ONLY_GLOBS%%` | JSON array of globs (string) | `["**/*.md","docs/**","LICENSE","NOTICE"]` | Whole-PR docs-only short-circuit. When **every** changed file matches one of the globs, the gate skips. Pass a valid JSON array literal; the workflow parses it with `jq`/`json.loads`. `**` matches across path separators, `*` does not. |
| `%%NODE_VERSION%%` | string | `"20"` | Major Node version for the Codex CLI install. |
| `%%CODEX_INSTALL_CMD%%` | shell snippet | `npm install -g @openai/codex@latest` | Must leave a working `codex` (and any plugin script the review invocation needs) on `$PATH`. |
| `%%CODEX_REVIEW_CMD%%` | shell snippet | `codex exec --json --output-schema .codex-review/schema.json --sandbox read-only "Run an adversarial review of the diff against origin/$BASE_REF"` | Must write a JSON document conforming to the openai-codex `review-output.schema.json` to `.codex-review/codex-review.json`. Stderr is captured separately. The workflow wraps a fallback so an invalid/missing JSON still produces a parseable artefact, but the command should normally succeed. |
| `%%BYPASS_LABEL%%` | string | `merge-gate-bypass` | Label that, when applied to a PR, causes preflight to short-circuit and the gate to pass without running Codex. Surfaced in the bypass sticky comment and check-outcome notice. Logged as an audited bypass — see operations playbook §4. Default `merge-gate-bypass` is fine for most projects; override if a project's label-namespacing convention demands a different name. |

## Substitution rules

- Substitute by literal text replace — do not try to JSON-encode or
  shell-quote the values yourself. The template already places each token in
  a context that accepts the documented type (single-quoted YAML scalar,
  shell-script body, etc.).
- For `%%DOCS_ONLY_GLOBS%%`, render a compact JSON array (e.g.
  `["**/*.md","docs/**"]`) — the surrounding YAML is single-quoted so the
  double quotes inside survive unchanged.
- For multi-line shell snippets (`%%CODEX_INSTALL_CMD%%`,
  `%%CODEX_REVIEW_CMD%%`) keep the indentation matching the surrounding
  `run: |` block. The installer should emit lines indented to match the
  token's column.

## Secrets the rendered workflow expects

- `OPENAI_API_KEY` *(or)* `CODEX_API_KEY` — whichever the chosen
  `%%CODEX_REVIEW_CMD%%` reads. At least one is required by the
  Codex adversarial-review step.
- `CLAUDE_CODE_OAUTH_TOKEN` — read by the `Run Claude validator`
  step, which invokes `claude -p "/run-codex-validators ..."` to
  classify each Codex finding as `uphold` / `dismiss` / `unsure`.
  Required under both soft and hard mode (the gate cannot make a
  blocking decision without a validator verdict).

`/setup-merge-gate` prints both names at the end of install as a
reminder to add them to the repo's Actions secrets.
