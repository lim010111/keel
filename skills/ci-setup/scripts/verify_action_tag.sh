#!/usr/bin/env bash
# Verify that a GitHub Action tag actually resolves before /ci-setup
# writes it into a workflow YAML. Read-only, no auth required.
#
# Usage:
#   verify_action_tag.sh <owner/repo>@<tag>
#
# Examples:
#   verify_action_tag.sh actions/checkout@v4
#   verify_action_tag.sh astral-sh/setup-uv@v8         # fails since v8.0.0
#   verify_action_tag.sh astral-sh/setup-uv@v8.1.0
#
# Exit codes:
#   0   tag resolves; prints "OK <repo>@<tag>" to stdout
#   1   tag missing; prints "MISSING <repo>@<tag>" to stderr and the best
#       fallback ("FALLBACK <repo>@<tag>") on the next line if one exists
#   2   usage error / network error
#
# Designed for the case that bit /ci-setup in 2026: astral-sh/setup-uv
# stopped publishing the rolling "vN" major tag from v8.0.0 onward, so
# `astral-sh/setup-uv@v8` silently breaks at first CI run. The same trap
# applies to any action that switches to immutable tags.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: verify_action_tag.sh <owner/repo>@<tag>" >&2
  exit 2
fi

INPUT="$1"
REPO="${INPUT%@*}"
TAG="${INPUT##*@}"

if [ "$REPO" = "$TAG" ] || [ -z "$REPO" ] || [ -z "$TAG" ]; then
  echo "usage: verify_action_tag.sh <owner/repo>@<tag>" >&2
  exit 2
fi

URL="https://github.com/${REPO}.git"
if ! REFS=$(git ls-remote --tags --refs "$URL" 2>/dev/null); then
  echo "ERROR: cannot reach ${URL}" >&2
  exit 2
fi

TAGS=$(echo "$REFS" | awk '{print $2}' | sed 's|refs/tags/||')

if echo "$TAGS" | grep -qx "$TAG"; then
  echo "OK ${REPO}@${TAG}"
  exit 0
fi

echo "MISSING ${REPO}@${TAG}" >&2

# Same-major fallback: if requested "v8" or "v8.1.0", prefer the latest
# tag in the v8.x.y series. If no such tag exists, fall back to the
# absolute latest semver-looking tag in the repo.
MAJOR=$(echo "$TAG" | grep -oE '^v?[0-9]+' || true)
SAME_MAJOR=""
if [ -n "$MAJOR" ]; then
  SAME_MAJOR=$(echo "$TAGS" \
    | grep -E "^${MAJOR}(\.[0-9]+){1,2}\$" \
    | sort -V \
    | tail -1 || true)
fi
ABS_LATEST=$(echo "$TAGS" \
  | grep -E '^v?[0-9]+(\.[0-9]+){1,2}$' \
  | sort -V \
  | tail -1 || true)

if [ -n "$SAME_MAJOR" ]; then
  echo "FALLBACK ${REPO}@${SAME_MAJOR}" >&2
elif [ -n "$ABS_LATEST" ]; then
  echo "FALLBACK ${REPO}@${ABS_LATEST}" >&2
fi
exit 1
