# Reference

## CLIs

### Codex

```
codex exec --sandbox read-only --skip-git-repo-check - < prompt.md
```

- `--sandbox read-only` enforces the no-mutation contract at the runtime level. Always pass it.
- `--skip-git-repo-check` is required when the working directory is not a git repo (e.g. `$HOME`). It is safe to always include because the sandbox already prevents writes — the git check is redundant under read-only.
- For prompts longer than a couple hundred chars, prefer stdin (`- < prompt.md`). Inline arg quoting breaks beyond ~500 chars.
- Codex's raw stdout streams its tool calls, file reads, intermediate drafts, and finally the final answer. The extractor in `run.sh` strips this trace by keeping only the slice from the LAST `## Verdict` line to EOF.
- Resume / session flags are not used here — each consultation is a fresh, isolated session.

### agy

```
agy -p <PROMPT>
```

- `-p` / `--print` runs a single prompt non-interactively and prints the response.
- agy has **no read-only flag**. The read-only contract must live in the prompt itself — see the `# Read-only contract` block in [PROMPT_TEMPLATE.md](PROMPT_TEMPLATE.md). This mirrors `third-party-review`'s posture toward agy.
- Do **not** pass `--dangerously-skip-permissions`.
- For long prompts, pass the prompt as a single argument: `agy -p "$(cat prompt.md)"`. `-p` does not read stdin natively.
- agy typically produces a clean report directly (no tool-trace prefix); extraction is still applied for symmetry and to validate section presence.

## Slice extractor (`scripts/prepare_slice.py`)

Usage:

```
python3 scripts/prepare_slice.py <slug> [--session <id>] [--jsonl <path>]
```

Reads the live session JSONL (located by cwd-encoded path under
`~/.claude/projects/`, newest-mtime tiebreaker, matching `third-party-review`'s
locator). Reconstructs the active conversation path from `leafUuid → root` to
drop rewound branches. Then picks a slice boundary using this contract:

1. **Primary marker** — backward scan on the active path for a `Skill`
   tool_use whose `input.skill` is one of `{grill-me, grill-with-docs}`.
   If found, slice starts at that event.
2. **Backward limit** — give up the marker search after `MAX_BACKWARD_TURNS`
   (= 15) human turns walked.
3. **Fallback** — if no marker is found within the limit, slice is the
   last `FALLBACK_TURNS` (= 5) human turns.

Slash-command-name strings (e.g. `<command-name>/grill-me</command-name>`)
and generic text matching (`grill`, `grilling`) are **not** used. Both were
empirically shown unreliable: the Skill-tool launch is the only system-enforced
contract, and free-text matching false-positives on quoted occurrences.

What goes into the rendered slice:

- Real human turns (after stripping `<system-reminder>`, `<task-notification>`,
  and the harness-injected `Base directory for this skill: ...` skill
  descriptions).
- AI text outputs.
- AI `thinking` blocks, truncated to `THINKING_MAX_CHARS` (= 600) chars.
- `Skill` tool_use entries (rendered as `### 🔧 Skill: <name>` transitions).

What is intentionally omitted:

- All non-`Skill` `tool_use` blocks and their `tool_result` bodies. The
  slice is about what was **said**, not what was **done**.
- File-read contents, command outputs, intermediate analyses.

Output:

- Writes `.scratch/council/<slug>-excerpt.md` (the rendered markdown).
- Prints a JSON stats blob to stdout (`excerpt`, `source_jsonl`,
  `active_path_length`, `slice_start_index`, `slice_length`,
  `boundary_mode`, `marker_skill`, `rendered_chars`).
- `boundary_mode` is `"marker"` (good), `"fallback"` (degraded — note it),
  or `"empty"` (active path empty — escalate).

Exit codes: `0` on success, non-zero on locator failure or
active-path-reconstruction failure (no `last-prompt` `leafUuid` in JSONL).

## Dispatcher + extractor (`scripts/run.sh`)

Usage:

```
scripts/run.sh <prompt-file> <slug>
```

Where `<slug>` is typically a timestamp (`20260525-143012`).

Pipeline:

1. Resolves the output directory to `.scratch/council/` under the current working directory; creates it if missing.
2. Launches two background processes:
   - `codex exec --sandbox read-only --skip-git-repo-check - < <prompt-file> > .scratch/council/<slug>-codex.raw.md 2>&1`
   - `agy -p "$(cat <prompt-file>)" > .scratch/council/<slug>-agy.raw.md 2>&1`
3. Prints the two PIDs.
4. **Blocks via `wait`** until both subprocesses finish, captures each exit code.
5. **Extracts a clean final report** from each raw file:
   - Finds the LAST `## Verdict` line.
   - `tail -n +<lineno>` from there to EOF, written to `<slug>-<agent>.md`.
   - Validates that the slice contains all required headers (`## Verdict`, `## Reasoning`, `## Per-option assessment`, `## Risks I cannot evaluate`).
   - If the file is empty, has no `## Verdict`, or is missing required sections, the clean file is replaced with a `FAILED:` marker pointing to the raw file.
6. Prints both exit codes and both file paths, then exits 0.

Because the skill invokes the script via `Bash(run_in_background=true)`, the harness's single completion notification corresponds to "both clean reports are ready". Do not poll or sleep — wait for that one notification, then `Read` both `.md` files (NOT the `.raw.md` files).

The script always exits 0 even if a subprocess failed; the caller detects partial failure by reading the `FAILED:` line from the clean file or by inspecting the printed exit codes.

## Why two files per agent (raw + clean)

Codex in particular streams its full reasoning trace — tool calls, intermediate analyses, draft verdicts — before producing the final answer. A real run can be 100–200 KB / 2000+ lines. Pulling all of that into the Main Session's context just to extract one 20-line verdict block is wasteful and noisy.

The split mirrors `third-party-review`'s discipline: present a verified evaluation to the consumer, keep the raw artifact on disk for audit. Here the consumer is the Main agent, not the human, so the cleaning step is essential — agents pay attention costs in tokens.

If a clean file looks wrong, open the corresponding `.raw.md` to see what the agent actually did. The raw is the source of truth for debugging; the clean is the contract for consumption.

## Manual fallback

If the dispatcher script is missing or broken, run each CLI directly using two `Bash` calls in a single message, both with `run_in_background=true`:

```
codex exec --sandbox read-only --skip-git-repo-check \
  - < .scratch/council/<TS>-prompt.md \
  > .scratch/council/<TS>-codex.raw.md 2>&1
```

```
agy -p "$(cat .scratch/council/<TS>-prompt.md)" \
  > .scratch/council/<TS>-agy.raw.md 2>&1
```

Then extract by hand:

```
LAST=$(awk '/^## Verdict[[:space:]]*$/{n=NR} END{print n+0}' <raw>)
tail -n +"$LAST" <raw> > <clean>
```

Validate the clean file contains the four required section headers. If anything is off, surface that to the Main Session — do not present the raw as if it were a report.

## Time and cost

- Both CLIs take meaningful wall time (often 30 s – 5 min). That is the entire reason for parallel dispatch.
- Each consultation spends external-API quota on both Codex and agy. Skip the skill for trivial decisions.
- A single grilling session may invoke `consult-externals` multiple times — once per genuinely hard branch — but the typical case is zero or one invocation.
