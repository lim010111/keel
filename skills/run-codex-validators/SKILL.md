---
name: run-codex-validators
description: Validator-layer runtime for the merge-gate. Reads Codex adversarial-review JSON, dispatches the `codex-review-validator` subagent to classify each finding (uphold/dismiss/unsure), then writes `.merge-gate/validators.{json,md}` for the merge-gate to consume. Invoked by the local merge-gate producer (`merge_gate_local.py produce`) or a human after a local `codex /adversarial-review`. Takes `--codex-json <path>` (default `./codex-review.json`) and `--soft-mode <true|false>`. Always exits 0 — the merge-gate's `verify` step is the sole authoritative gate. MVP is Claude-only (ADR-0005).
---

# `/run-codex-validators` — validator-layer runtime

You are the runtime glue between Codex (which produced JSON findings) and
the Claude validator subagent (which classifies them). Your output is two
files the merge-gate consumes; you do not decide the gate. The gate's
`verify` step does.

## Invocation contract

The caller invokes you as:

```
claude -p "/run-codex-validators --codex-json <path> --soft-mode <true|false>" \
  --permission-mode bypassPermissions
```

- Working directory is the target repo root.
- `--codex-json` default: `./codex-review.json`.
- `--soft-mode` is `true` or `false`; required. It is a **posture label only** —
  it selects the Mode line in `validators.md` and changes nothing the gate
  computes. The local producer derives it from the repo's `enforcement_policy`
  (`advisory` → `true`, `client-side-blocking` → `false`), so the artefact stops
  announcing "HARD (blocking)" on a repo that cannot block
  (claude-harness-work#58).
- `--out-dir` (optional) default `./.merge-gate/` — the directory the two
  output files are written to. The default sits inside the local profile's
  gitignored, review-scope-excluded artifact root (claude-harness-work#46),
  so a manual run leaves no committable artefact. The local merge-gate
  producer (`merge-gate-local produce`, claude-harness-work#30) passes a
  per-reviewer tuple sub-dir here so each reviewer's `validators.{json,md}`
  land separately. **Use `$OUT_DIR` below wherever an output path is
  needed — never hardcode one.**
- `--intent-from <path>` (optional) — a file of **durable** validator context
  (branch name / published-range commit messages / operator-supplied intent).
  The local profile has no PR body, so the producer supplies this written
  intent for the validator to weigh like a PR description (D11). Pass it
  through to `build-input` as `--durable-context-from` (step 4).
- `--reviewer <name>` (optional, claude-harness-work#57) — the reviewer that
  produced the findings (`codex`, `claude`, or a custom name). Reviewers are a
  **set** (ADR-0010), so the judge must not assume Codex. Pass it through to
  `build-input` (step 4). Absent (a manual invocation) → the payload omits the
  key and the agent judges with no reviewer assumption.
- `--changed-files-from <path>` (optional, claude-harness-work#55) — a file of
  changed paths, one per line, supplied by the **caller**. When present it is
  **authoritative**: use it verbatim and do not run `git diff` (step 3). The
  local producer always passes it — it already holds the canonical diff's path
  list, whereas this skill's own derivation silently yields an empty list on any
  repo whose default branch is not `main` and on every post-push run. Absent (a
  manual invocation) → derive once, and on failure drop the flag so the payload
  records `changed_files_status: "unavailable"`.
- `--agent-model <alias>` (optional, claude-harness-work#47) — tier alias
  (`haiku`/`sonnet`/`opus`) for the **validator agent** (the judgment
  subagent). The producer reads it from `[merge-gate.local.validator] model`
  in harness.toml and passes it here — this CLI arg is the only carrier (this
  skill reads no project config, see "What this skill must not do"). When
  absent, the agent definition's own frontmatter `model:` applies. Consumed
  in step 5.

## Always exit 0

This is the most important constraint. No matter what goes wrong —
missing Codex JSON, malformed input, subagent failure — write the
fallback artifacts (via `scripts/aggregate.py write-fallback`) and
return success. The merge-gate's `verify` step is the sole authoritative
gate (ADR-0005; composition — ADR-0011).

## Names here are historical

`run-codex-validators`, `codex-review-validator`, and the payload's `codex_json`
key all date from when Codex was the only reviewer. Reviewers have been a **set**
since ADR-0010 (`codex` + `claude` + custom), and this runtime is reviewer-neutral
— it validates whatever reviewer's findings it is handed, named by `--reviewer`.
The names are kept deliberately: renaming touches 12 real references across the
skill, agent, producer, three test suites, the keel mirror and its `.allowlist`,
and the producer composes the slash-command name as a string. Treat every `codex`
in an identifier here as "the reviewer", not "Codex" (claude-harness-work#57).

## Adapter table — the silent bug killer

The reviewer emits findings at `.result.findings[]` with a `line_start` field.
The validator agent's `<input>` block (see
`~/.claude/agents/codex-review-validator.md`) expects `.codex_json.findings[].line`.
Map per this table — `scripts/aggregate.py build-input` implements it:

| Reviewer `.result.findings[]` | Validator `<input>.codex_json.findings[]` | Notes |
|---|---|---|
| `id`            | `id`            | pass-through |
| `severity`      | `severity`      | pass-through (`critical|high|medium|low`) |
| `file`          | `file`          | pass-through |
| `line_start`    | `line`          | **renamed** — without this, the validator sees malformed input |
| `title`         | `title`         | pass-through |
| `body`          | `body`          | pass-through |
| `suggested_fix` | `suggested_fix` | pass-through (optional) |
| `line_end`      | *(dropped)*     | validator only reads `line` |
| *(`--reviewer` arg)* | `reviewer`  | **added** (#57) — which reviewer produced these findings; omitted when unsupplied, and the agent then assumes no provenance |
| *(the whole envelope)* | `codex_json` | **historical name** — holds an arbitrary reviewer's findings, not necessarily Codex's. Kept for schema compatibility; do NOT rename |

`project_refs` is hardcoded to the validator agent's documented defaults
(`AGENTS.md`, `docs/adr/*.md`, `CONTEXT-MAP.md`, `src/*/CONTEXT.md`).

## How aggregation works — pairing on finding id

The aggregator (`scripts/aggregate.py write-outputs`) takes two streams
— Codex's `findings[]` JSON and the validator agent's line-oriented
stdout — and writes one aggregate entry per finding. Pairing is
**identity-based** via the finding's `id`:

1. `cmd_build_input` hands each finding to the validator with an
   explicit `id` (synthesized as `finding-{i}` when Codex omits one).
2. The validator's `<output_contract>` requires each line to echo
   `id=<id>` verbatim (see `~/.claude/agents/codex-review-validator.md`).
3. `cmd_write_outputs` builds a `findings_by_id` lookup and resolves
   every parsed validator line by id — **not by position**. Order in
   the validator's stdout no longer matters for correctness.
4. After id resolution, the aggregator sanity-checks
   `(file, line, severity)` between the Codex finding and the parsed
   line. Mismatch demotes that finding to `unsure` with an `stderr`
   warning naming the id and the divergent fields.
5. Failure modes fail safely:
   - Non-unique id from Codex (two findings carrying the same `id`) →
     id-as-identity is broken at the source, so **every** colliding
     finding is forced to `unsure` *before* the verdict table is
     consulted, with an `stderr` warning naming the id and the count.
     The validator line is **not** consulted for those findings —
     otherwise one `dismiss` line could be reused across distinct
     findings (a hard-mode fail-open). Symmetric to the validator-side
     duplicate guard below (refs claude-harness-work#29).
   - Duplicate id from the validator → that finding becomes `unsure`
     + `stderr` warning naming the id and the count.
   - Validator line with an id matching no Codex finding → that
     parsed line becomes an `orphan-i` aggregate entry with
     `block=false`, regardless of the validator-supplied severity or
     verdict. The validator's scope contract is **classify Codex
     findings, not author new ones**; trusting an orphan's
     severity/verdict to drive `decide_block` would give the validator
     a side channel to invent its own blockers (refs
     claude-harness-work#28). The unclaimed Codex finding (if any)
     falls into the existing "validator output missing" fail-safe.
   - Codex finding with no validator line claiming its id → the
     existing "validator output missing" fail-safe (preserves the
     `#22` parity-check behavior).

Why identity-based: ADR-0008 records the decision. The previous
positional pairing (`findings[i]` ↔ `parsed[i]`) silently
mis-applied verdicts when the validator agent reordered its lines —
a HIGH `uphold` could swap with a LOW `dismiss` and the gate would
wave the high finding through. Identity-based pairing makes that
class of defect structurally impossible; the sanity check catches
the remaining failure mode (model echoes the wrong id but otherwise
plausible attributes).

## Verdict → block table (single-model MVP)

Per finding, the aggregator computes `block` from Codex severity × Claude
verdict per ADR-0005:

| Codex severity   | Claude verdict | `block` |
|---|---|---|
| critical / high  | `uphold`  | `true` |
| critical / high  | `unsure`  | `true` (fail-safe; human applies `merge-gate-bypass` label) |
| critical / high  | `dismiss` | `false` |
| medium / low     | (any)     | `false` |

The list-shaped output schema (`{"validators": [...], "aggregate": [...]}`)
is the ADR-0005 forward-compat anchor — adding a second validator later
is `append`, not a rewrite.

## Output files

Written under `$OUT_DIR` (default `./.merge-gate/`, created if missing):

- `validators.json`:
  ```json
  {
    "validators": [{"name": "claude", "lines": ["[HIGH] uphold src/foo.ts:42 — citation", "..."]}],
    "aggregate":  [{"finding_id": "f1", "severity": "high", "verdict": "uphold", "block": true}, "..."]
  }
  ```
- `validators.md` — severity-count table + the list of items where
  `block == true` with their one-line citations — the human-readable
  companion the merge-gate surfaces in its report.

## Where `aggregate.py` lives

The helper script lives alongside this SKILL.md, at `scripts/aggregate.py`
within the skill directory — the global install:
`~/.claude/skills/run-codex-validators/scripts/aggregate.py`. (Per-repo
vendored copies were a github-actions-profile mechanism — removed,
ADR-0021.) Below, `$AGG` stands for that resolved path;
`python3 "$AGG" <subcommand> …` is the call shape.

## Workflow you execute

1. **Parse arguments.** Extract `--codex-json` (default
   `./codex-review.json`), `--soft-mode`, `--out-dir` (default
   `.merge-gate` → call it `$OUT_DIR`), the optional `--intent-from`
   (call it `$INTENT_FROM`, unset if absent), the optional
   `--changed-files-from` (call it `$CHANGED_FILES_FROM`, unset if absent),
   the optional `--reviewer` (call it `$REVIEWER`, unset if absent), and
   the optional `--agent-model` (call it `$AGENT_MODEL`, unset if
   absent) from the slash-command invocation. If `--soft-mode` is missing
   or not `true|false`, run
   `python3 "$AGG" write-fallback --reason "soft-mode flag missing or invalid" --out-dir "$OUT_DIR"`
   and return.

2. **Pre-flight on Codex JSON.** If the file at `--codex-json` is missing
   or `jq -e . <path>` fails, run
   `python3 "$AGG" write-fallback --reason "<concise reason>" --out-dir "$OUT_DIR"`
   (`"codex JSON missing at <path>"` or `"codex JSON failed to parse"`),
   then return.

3. **Resolve `changed_files` — do NOT re-derive it when the caller gave you
   one.** Run this block verbatim:

   ```bash
   if [ -n "$CHANGED_FILES_FROM" ]; then
     # The producer passed the AUTHORITATIVE list (claude-harness-work#55).
     # Do NOT run git diff. Do NOT second-guess it, even if it looks empty.
     CF_ARG="--changed-files-from $CHANGED_FILES_FROM"
   else
     # Manual run only. Derive once; if it fails, DROP THE FLAG (never write an
     # empty file) so build-input records changed_files_status=unavailable.
     CHANGED_FILES_TMP="$(mktemp)"
     BASE_REF="${BASE_REF:-${GITHUB_BASE_REF:-main}}"
     if git diff --name-only "origin/${BASE_REF}" > "$CHANGED_FILES_TMP" 2>/dev/null; then
       CF_ARG="--changed-files-from $CHANGED_FILES_TMP"
     else
       CF_ARG=""
       echo "run-codex-validators: could not derive changed_files from origin/${BASE_REF}" >&2
     fi
   fi
   ```

   Never abort on a derivation failure — an empty `CF_ARG` is a valid outcome
   and the payload will say so.

4. **Build validator input.** Run this block verbatim:

   ```bash
   INPUT_TMP="$(mktemp)"
   ISSUE_REF="${GITHUB_PR_NUMBER:+PR #${GITHUB_PR_NUMBER}}"
   ISSUE_REF="${ISSUE_REF:-branch ${GITHUB_HEAD_REF:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}}"
   python3 "$AGG" build-input \
     --codex-json "$CODEX_JSON" \
     --issue-ref  "$ISSUE_REF" \
     ${REVIEWER:+--reviewer "$REVIEWER"} \
     ${INTENT_FROM:+--durable-context-from "$INTENT_FROM"} \
     $CF_ARG > "$INPUT_TMP"
   ```

   The `${VAR:+…}` expansions are how an absent `--reviewer` / `--intent-from`
   becomes an omitted `reviewer` / `durable_context` key — do not substitute a
   placeholder value for either.

5. **Dispatch the validator subagent.** Use the Agent tool with
   `subagent_type: codex-review-validator`. **Read `$INPUT_TMP` and pass its
   CONTENTS inline as the agent prompt body** — never pass the path. (A path
   makes the payload the subagent's problem to fetch, and fixed-name temp files
   collide between concurrent produces.) **If `$AGENT_MODEL` is set**, also pass
   it as the Agent tool's `model` parameter; when unset, omit the parameter so
   the agent definition's frontmatter default applies (#47). The subagent runs
   in its own context window per PRD design intent. Write its full response to
   `$VALIDATOR_OUT_TMP` (`mktemp`).

6. **Write outputs.** Run
   `python3 "$AGG" write-outputs --codex-json "$CODEX_JSON" --validator-output "$VALIDATOR_OUT_TMP" --soft-mode "$SOFT_MODE" --out-dir "$OUT_DIR"`.

7. **Report and return.** Print **at most two lines** naming the two output
   files. No markdown report, no severity table, no summary of the verdicts, no
   restatement of what you did — the artefacts ARE the output and the merge-gate
   reads them from disk. Nothing you write here is read by anything
   (claude-harness-work#56). Do not return non-zero.

## Error handling

If any step fails unexpectedly (subagent fails to respond, script returns
non-zero, JSON malformed mid-run), run the fallback path:

```
python3 "$AGG" write-fallback \
  --reason "<short concrete reason>" \
  --out-dir "$OUT_DIR"
```

then return success. The merge-gate's `verify` step handles the rest.

The fallback writes `validators.json` with `aggregate: []` and a non-empty
`fallback: "<reason>"` key. The merge-gate is the sole gate decision-maker, and
an empty `aggregate[]` is all it needs: `build_summary`'s F2 fail-safe gives
every finding with no aggregate entry a `unsure` verdict, which blocks at
critical/high under blocking enforcement (claude-harness-work#24), and records
`validator_ran: false` so the archive can tell that fail-safe from a real
verdict (#58). The runtime's "always exit 0" contract is preserved — do NOT
change `write-fallback` to embed findings into `aggregate[]`; the gate already
has the information it needs to decide.

## What this skill must not do

- Do not call `agy`, `gemini`, or any validator beyond
  `codex-review-validator`. ADR-0005 keeps the MVP single-model (a 2nd
  validator is post-v1 backlog).
- Do not change `validators.json` to a dict-shaped schema. The list
  shape is the forward-compat anchor.
- Do not rewrite the validator agent's `<input>` / `<output_contract>`.
  Adapt at this skill's boundary (the adapter table above).
- Do not return non-zero from this skill or its scripts. The merge-gate's
  `verify` step decides blocking; this runtime only produces evidence.
- Do not read `harness.toml` or other project config. The CLI args
  carry everything needed; configuration is the installer's concern (#04).

## See also

- `~/.claude/agents/codex-review-validator.md` (#02) — the validator's
  `<input>` and `<output_contract>` this skill adapts to and parses from.
- `~/.claude/scripts/merge_gate_local.py` (#30) — the producer that
  invokes this skill headless and the `verify` step that consumes its two
  output files.
- ADR-0005 `claude-only-validator-mvp-gemini-deferred.md` — the rationale
  for single-validator MVP and list-shaped schema.
