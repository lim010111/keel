#!/usr/bin/env bash
# Regression tests for scan.sh discovery + classification hardening (findings #51-adjacent
# review of the matt-drift-watch skill). Runs scan.sh fully OFFLINE against fixture roots
# via the MDW_* seams — no network, no touching the real ~/.claude or ~/.agents.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; SCAN="$HERE/scan.sh"
pass=0; fail=0

fixture()   { local d; d="$(mktemp -d)"; mkdir -p "$d/claude/skills" "$d/agents/skills" "$d/cache/skills"; echo "$d"; }
run_scan()  { MDW_SKIP_FETCH=1 MDW_OURS_ROOT="$1/claude/skills" MDW_AGENTS_ROOT="$1/agents/skills" MDW_CACHE="$1/cache" bash "$SCAN" 2>&1; }
mk_upstream(){ mkdir -p "$1/cache/skills/$2/$3"; printf '%s' "${4:-same}" > "$1/cache/skills/$2/$3/SKILL.md"; }
mk_agents() { mkdir -p "$1/agents/skills/$2"; printf '%s' "${3:-same}" > "$1/agents/skills/$2/SKILL.md"; }
mk_symlink(){ ln -s "$1/agents/skills/$2" "$1/claude/skills/$2"; }
have()      { if grep -qE "$3" <<<"$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1 — expected /$3/"; echo "$2" | sed 's/^/    | /'; fi; }
lack()      { if grep -qE "$3" <<<"$2"; then fail=$((fail+1)); echo "FAIL: $1 — did NOT expect /$3/"; echo "$2" | sed 's/^/    | /'; else pass=$((pass+1)); fi; }

# T0 — behavior preserved: valid rented skill identical to upstream → IDENTICAL, not NEW
F=$(fixture); mk_upstream "$F" engineering foo; mk_agents "$F" foo; mk_symlink "$F" foo
out=$(run_scan "$F"); have "T0 identical" "$out" "IDENTICAL[[:space:]]+foo"; lack "T0 not-new" "$out" "NEW[[:space:]]+foo"; rm -rf "$F"

# T1 (finding ① codex) — rented skill with MISSING .claude/skills symlink → MISSING-SYMLINK, not false NEW
F=$(fixture); mk_upstream "$F" engineering bar; mk_agents "$F" bar
out=$(run_scan "$F"); have "T1 missing-symlink" "$out" "MISSING-SYMLINK[[:space:]]+bar"; lack "T1 not-false-new" "$out" "NEW[[:space:]]+bar"; rm -rf "$F"

# T1b (finding ①) — dangling symlink (target deleted) → BROKEN-SYMLINK, not silent empty DIFFERS
F=$(fixture); mk_upstream "$F" engineering baz; mk_agents "$F" baz; mk_symlink "$F" baz; rm -rf "$F/agents/skills/baz"
out=$(run_scan "$F"); have "T1b broken-symlink" "$out" "BROKEN-SYMLINK[[:space:]]+baz"; rm -rf "$F"

# T2 (finding ② claude) — rented skill upstream-moved to a NON-watched category → matched + flagged, not dropped/NEW
F=$(fixture); mk_upstream "$F" deprecated qux; mk_agents "$F" qux; mk_symlink "$F" qux
out=$(run_scan "$F"); have "T2 moved-flagged" "$out" "qux.*moved to deprecated"; lack "T2 not-new" "$out" "NEW[[:space:]]+qux"; rm -rf "$F"

# T3 (finding ③ claude) — same name in two categories → collision WARN, not silent overwrite
F=$(fixture); mk_upstream "$F" engineering dup; mk_upstream "$F" productivity dup
out=$(run_scan "$F"); have "T3 collision-warn" "$out" "[Cc]ollision.*dup"; rm -rf "$F"

echo "---- $pass passed, $fail failed ----"; [ "$fail" -eq 0 ]
