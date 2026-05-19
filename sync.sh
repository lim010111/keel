#!/usr/bin/env bash
#
# keel/sync.sh — mirror authored content from ~/.claude into this repo.
#
# keel is a downstream mirror; ~/.claude is the source of truth (see
# docs/adr/0001). Workflow: edit in ~/.claude, run this, commit here.
#
#   ./sync.sh            # sync from ~/.claude
#   CLAUDE_DIR=... ./sync.sh   # sync from a different Claude config dir
#
set -euo pipefail

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALLOWLIST="$REPO_DIR/.allowlist"

[[ -d "$CLAUDE_DIR" ]] || { echo "error: $CLAUDE_DIR not found" >&2; exit 1; }
[[ -f "$ALLOWLIST" ]] || { echo "error: $ALLOWLIST not found" >&2; exit 1; }

# Lockstep: setup-status-harness ships its own copy of status.py. Keep it
# equal to the canonical generator in BOTH trees so the harness skill never
# installs a stale version (see docs/adr/0001).
cp "$CLAUDE_DIR/scripts/status.py" \
   "$CLAUDE_DIR/skills/setup-status-harness/scripts/status.py"

EXCLUDES=(
  --exclude='*:Zone.Identifier'   # Windows "downloaded from internet" cruft
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.tdd-state/'
  --exclude='.tdd-markers/'
  --exclude='.session-devlog/'
  --exclude='CREATION_PROMPT.md'  # skill-creator build artifact, not content
)

echo "Syncing authored content: $CLAUDE_DIR -> $REPO_DIR"
while IFS= read -r entry; do
  [[ -z "$entry" || "$entry" == \#* ]] && continue
  src="$CLAUDE_DIR/$entry"
  dst="$REPO_DIR/$entry"
  if [[ ! -e "$src" ]]; then
    echo "  WARN: missing $src — skipped" >&2
    continue
  fi
  mkdir -p "$(dirname "$dst")"
  if [[ -d "$src" ]]; then
    rsync -a --delete "${EXCLUDES[@]}" "$src/" "$dst/"
  else
    rsync -a "${EXCLUDES[@]}" "$src" "$dst"
  fi
  echo "  synced $entry"
done < "$ALLOWLIST"

echo "Done. Review with: git -C \"$REPO_DIR\" status"
