#!/usr/bin/env bash
# matt-drift-watch — mechanical scan. Advisory only: fetches upstream HEAD and diffs our
# rented Matt Pocock skills against it. Reads only; never writes to our skills.
#
# Test/offline seams (all optional; defaults = the real harness):
#   MDW_OURS_ROOT    our skills dir       (default ~/.claude/skills)
#   MDW_AGENTS_ROOT  rented layer         (default ~/.agents/skills)
#   MDW_CACHE        upstream clone dir   (default $TMPDIR/matt-drift-watch-cache)
#   MDW_SKIP_FETCH   set=1 → skip the network fetch, use MDW_CACHE as-is
#   MDW_WATCHED      categories to surface NEW from (default "engineering productivity")
set -euo pipefail
shopt -s nullglob

REPO="https://github.com/mattpocock/skills.git"
BRANCH=main
CACHE="${MDW_CACHE:-${TMPDIR:-/tmp}/matt-drift-watch-cache}"
OURS_ROOT="${MDW_OURS_ROOT:-$HOME/.claude/skills}"
AGENTS_ROOT="${MDW_AGENTS_ROOT:-$HOME/.agents/skills}"
WATCHED="${MDW_WATCHED:-engineering productivity}"   # categories we surface NEW candidates from

is_watched() { case " $WATCHED " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# 1. fetch upstream HEAD (cheap re-runs via cached shallow clone; skip for offline/tests)
if [ -z "${MDW_SKIP_FETCH:-}" ]; then
  if [ -d "$CACHE/.git" ]; then
    git -C "$CACHE" fetch --depth 1 origin "$BRANCH" >/dev/null 2>&1
    git -C "$CACHE" reset --hard FETCH_HEAD >/dev/null 2>&1
  else
    rm -rf "$CACHE"
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$CACHE" >/dev/null 2>&1
  fi
  echo "== upstream HEAD: $(git -C "$CACHE" log -1 --format='%h %ci %s')"
else
  echo "== upstream HEAD: (fetch skipped — using cache $CACHE)"
fi
echo "== diff legend:  '<' = our rented copy   '>' = upstream HEAD"
echo

UP="$CACHE/skills"

# Map every upstream skill across ALL categories, so a rented skill upstream-moved out of
# the watched set is still found (not silently dropped). Warn on cross-category name
# collisions instead of silently overwriting.
declare -A up_path=() up_cat=()
for d in "$UP"/*/; do
  cat="$(basename "$d")"
  for s in "$d"*/; do
    name="$(basename "$s")"
    if [ -n "${up_path[$name]:-}" ]; then
      echo "  WARN  collision: '$name' in ${up_cat[$name]}/ and $cat/ — keeping a watched copy"
      if is_watched "$cat"; then up_path[$name]="$s"; up_cat[$name]="$cat"; fi
      continue
    fi
    up_path[$name]="$s"; up_cat[$name]="$cat"
  done
done

# 2. diff each of our rented-layer skills against its upstream copy (wherever it now lives)
declare -A matched=()
echo "== rented skills =="
for entry in "$OURS_ROOT"/*; do
  name="$(basename "$entry")"
  if [ -L "$entry" ] && [ ! -e "$entry" ]; then            # dangling symlink → flag, don't misclassify
    echo "  BROKEN-SYMLINK  $name -> $(readlink "$entry")"
    matched["$name"]=1
    continue
  fi
  tgt="$(readlink -f "$entry" 2>/dev/null || true)"
  case "$tgt" in "$AGENTS_ROOT"/*) ;; *) continue ;; esac   # only rented-layer symlinks
  up="${up_path[$name]:-}"
  [ -n "$up" ] || continue                                  # upstream doesn't have it (not Matt's / deleted upstream)
  matched["$name"]=1
  note=""
  if ! is_watched "${up_cat[$name]}"; then note="   [upstream moved to ${up_cat[$name]}/]"; fi
  if diff -rq "$tgt" "$up" >/dev/null 2>&1; then
    echo "  IDENTICAL  $name$note"
  else
    echo "  DIFFERS    $name$note"
    diff -r "$tgt" "$up" 2>/dev/null | sed 's/^/      /' || true
  fi
done

# 3. watched-category upstream skills with no local match: tell a broken/missing rented
#    symlink (we DO rent it) apart from a genuinely new skill.
echo
echo "== upstream-only (NEW) =="
for name in "${!up_path[@]}"; do
  [ -n "${matched[$name]:-}" ] && continue
  is_watched "${up_cat[$name]}" || continue                # keep NEW scoped to watched categories
  if [ -e "$AGENTS_ROOT/$name" ]; then
    echo "  MISSING-SYMLINK  $name  (rented layer has it, but no valid $OURS_ROOT/$name symlink)"
  else
    echo "  NEW  $name  (${up_cat[$name]}/$name)"
  fi
done | sort
