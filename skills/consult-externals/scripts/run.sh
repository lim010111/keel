#!/usr/bin/env bash
# Dispatch a single grilling-decision prompt to Codex and agy in parallel,
# wait until both finish, then extract a clean final report from each
# raw output. The Main Session reads the clean files only.
#
# File layout (under ./.scratch/council/, relative to CWD):
#   <slug>-prompt.md     # the shared prompt (caller-written)
#   <slug>-codex.raw.md  # full Codex stdout/stderr (tool trace, drafts, etc.)
#   <slug>-agy.raw.md    # full agy   stdout/stderr
#   <slug>-codex.md      # CLEAN final report — what the Main Session reads
#   <slug>-agy.md        # CLEAN final report
#
# A clean report is the slice from the LAST occurrence of `## Verdict` to EOF.
# If that slice does not contain all required section headers, the clean file
# is replaced with a one-line FAILED marker that points back to the raw file.
#
# Usage: scripts/run.sh <prompt-file> <slug>
#
# The script BLOCKS until both subprocesses finish and both extractions run.
# When invoked via Bash(run_in_background=true), the harness's single
# completion notification means "both clean reports are ready".

set -uo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <prompt-file> <slug>" >&2
  exit 2
fi

PROMPT_FILE="$1"
SLUG="$2"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "prompt file not found: $PROMPT_FILE" >&2
  exit 2
fi

OUT_DIR=".scratch/council"
mkdir -p "$OUT_DIR"

CODEX_RAW="$OUT_DIR/${SLUG}-codex.raw.md"
AGY_RAW="$OUT_DIR/${SLUG}-agy.raw.md"
CODEX_CLEAN="$OUT_DIR/${SLUG}-codex.md"
AGY_CLEAN="$OUT_DIR/${SLUG}-agy.md"

# Codex:
#   --sandbox read-only enforces the no-mutation contract at runtime.
#   --skip-git-repo-check lets codex run from a non-git directory (e.g. $HOME);
#     safe because the sandbox already prevents writes.
codex exec --sandbox read-only --skip-git-repo-check - < "$PROMPT_FILE" \
  > "$CODEX_RAW" 2>&1 &
CODEX_PID=$!

# agy: no read-only flag; the prompt itself carries the contract.
# -p does not read stdin, so we pass the prompt as a single argument.
agy -p "$(cat "$PROMPT_FILE")" > "$AGY_RAW" 2>&1 &
AGY_PID=$!

echo "codex pid=$CODEX_PID  -> $CODEX_RAW"
echo "agy   pid=$AGY_PID  -> $AGY_RAW"

wait "$CODEX_PID"; CODEX_RC=$?
wait "$AGY_PID";   AGY_RC=$?

# Required headers in a valid report (in any order; presence-checked only).
REQUIRED=("^## Verdict\$" "^## Reasoning\$" "^## Per-option assessment\$" "^## Risks I cannot evaluate\$")

extract_clean() {
  # extract_clean <raw_path> <clean_path> <label>
  local raw="$1" clean="$2" label="$3"
  if [[ ! -s "$raw" ]]; then
    printf 'FAILED: %s produced no output. See %s\n' "$label" "$raw" > "$clean"
    return
  fi
  local last
  last=$(awk '/^## Verdict[[:space:]]*$/{n=NR} END{print n+0}' "$raw")
  if [[ "$last" -le 0 ]]; then
    printf 'FAILED: %s output has no `## Verdict` section. See %s\n' "$label" "$raw" > "$clean"
    return
  fi
  tail -n +"$last" "$raw" > "$clean"
  # Validate the slice contains every required section header.
  local missing=()
  for pat in "${REQUIRED[@]}"; do
    grep -Eq "$pat" "$clean" || missing+=("$pat")
  done
  if (( ${#missing[@]} > 0 )); then
    {
      printf 'FAILED: %s output missing required sections: %s\n' "$label" "${missing[*]}"
      printf 'See %s for full trace. Partial extraction follows:\n\n' "$raw"
      cat "$clean"
    } > "${clean}.tmp"
    mv "${clean}.tmp" "$clean"
  fi
}

extract_clean "$CODEX_RAW" "$CODEX_CLEAN" "codex"
extract_clean "$AGY_RAW"   "$AGY_CLEAN"   "agy"

echo "codex exit=$CODEX_RC  clean=$CODEX_CLEAN  raw=$CODEX_RAW"
echo "agy   exit=$AGY_RC  clean=$AGY_CLEAN  raw=$AGY_RAW"
exit 0
