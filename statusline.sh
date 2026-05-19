#!/usr/bin/env bash
# Claude Code status line script
# Format: Model (ctx) | branch* | ctx% | 5h XX% (HhMm) | 7d XX% (DdHh)

input=$(cat)

# ── 1. Model segment ──────────────────────────────────────────────────────────
model_display=$(echo "$input" | jq -r '.model.display_name // "?"')
ctx_size=$(echo "$input" | jq -r '.context_window.context_window_size // 0')

# Build context-window label (e.g. "1M", "200K")
if [ "$ctx_size" -ge 1000000 ] 2>/dev/null; then
  ctx_label="$(( ctx_size / 1000000 ))M"
elif [ "$ctx_size" -ge 1000 ] 2>/dev/null; then
  ctx_label="$(( ctx_size / 1000 ))K"
else
  ctx_label="${ctx_size}"
fi

# Strip "Claude " prefix if present to shorten the name
model_short="${model_display#Claude }"

ORANGE='\033[38;5;215m'
GRAY='\033[38;5;245m'
RESET='\033[0m'

printf "${ORANGE}%s${RESET} ${GRAY}(%s)${RESET}" "$model_short" "$ctx_label"

# ── 2. Git branch segment ─────────────────────────────────────────────────────
YELLOW='\033[33m'

cwd=$(echo "$input" | jq -r '.workspace.current_dir // .cwd // empty')
if [ -n "$cwd" ]; then
  branch=$(git -C "$cwd" --no-optional-locks rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ -n "$branch" ]; then
    dirty=$(git -C "$cwd" --no-optional-locks status --porcelain 2>/dev/null)
    dirty_marker=""
    [ -n "$dirty" ] && dirty_marker="*"
    printf " ${GRAY}|${RESET} ${YELLOW}%s%s${RESET}" "$branch" "$dirty_marker"
  fi
fi

# ── 3. Session context usage % ────────────────────────────────────────────────
CYAN='\033[36m'

used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
if [ -n "$used_pct" ]; then
  used_int=$(printf "%.0f" "$used_pct")
  printf " ${GRAY}|${RESET} ${CYAN}%d%%${RESET}" "$used_int"
else
  printf " ${GRAY}|${RESET} ${CYAN}0%%${RESET}"
fi

# ── 4 & 5. ccusage 5h / 7d blocks ────────────────────────────────────────────
GREEN='\033[32m'
RED='\033[31m'

# Helper: pick color for a percentage value
pct_color() {
  local pct=$1
  if [ "$pct" -ge 80 ]; then
    printf '%s' "$RED"
  elif [ "$pct" -ge 50 ]; then
    printf '%s' "$YELLOW"
  else
    printf '%s' "$GREEN"
  fi
}

# Helper: convert epoch seconds to "Xh Ym" or "Xd Yh" remaining
fmt_remaining() {
  local resets_at=$1
  local now
  now=$(date +%s)
  local diff=$(( resets_at - now ))
  if [ "$diff" -le 0 ]; then
    printf "0m"
    return
  fi
  local days=$(( diff / 86400 ))
  local hours=$(( (diff % 86400) / 3600 ))
  local mins=$(( (diff % 3600) / 60 ))
  if [ "$days" -gt 0 ]; then
    printf "%dd%dh" "$days" "$hours"
  else
    printf "%dh%dm" "$hours" "$mins"
  fi
}

# ── 4 & 5. Rate limit blocks ─────────────────────────────────────────────────
# Priority 1: use rate_limits injected directly by Claude Code into stdin
five_pct=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
five_resets=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
seven_pct=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')
seven_resets=$(echo "$input" | jq -r '.rate_limits.seven_day.resets_at // empty')

# Priority 2: fall back to ccusage if Claude Code did not provide the fields
if [ -z "$five_pct" ] && [ -z "$seven_pct" ]; then
  ccusage_json=""
  if command -v ccusage &>/dev/null; then
    ccusage_json=$(ccusage statusline 2>/dev/null)
  elif command -v npx &>/dev/null; then
    ccusage_json=$(npx --yes ccusage@latest statusline 2>/dev/null)
  fi

  if [ -n "$ccusage_json" ] && echo "$ccusage_json" | jq -e . &>/dev/null; then
    # ccusage may use camelCase keys; try both snake_case and camelCase
    five_pct=$(echo "$ccusage_json" | jq -r '.five_hour.used_percentage // .fiveHour.usedPercentage // empty' 2>/dev/null)
    five_resets=$(echo "$ccusage_json" | jq -r '.five_hour.resets_at // .fiveHour.resetsAt // empty' 2>/dev/null)
    seven_pct=$(echo "$ccusage_json" | jq -r '.seven_day.used_percentage // .sevenDay.usedPercentage // empty' 2>/dev/null)
    seven_resets=$(echo "$ccusage_json" | jq -r '.seven_day.resets_at // .sevenDay.resetsAt // empty' 2>/dev/null)
  fi
fi

# ── 5-hour block ──
if [ -n "$five_pct" ]; then
  five_int=$(printf "%.0f" "$five_pct")
  five_color=$(pct_color "$five_int")
  if [ -n "$five_resets" ]; then
    five_rem=$(fmt_remaining "$five_resets")
    printf " ${GRAY}|${RESET} ${GRAY}5h${RESET} ${five_color}%d%%${RESET} ${GRAY}(%s)${RESET}" "$five_int" "$five_rem"
  else
    printf " ${GRAY}|${RESET} ${GRAY}5h${RESET} ${five_color}%d%%${RESET}" "$five_int"
  fi
else
  printf " ${GRAY}|${RESET} ${GRAY}5h --${RESET}"
fi

# ── 7-day block ──
if [ -n "$seven_pct" ]; then
  seven_int=$(printf "%.0f" "$seven_pct")
  seven_color=$(pct_color "$seven_int")
  if [ -n "$seven_resets" ]; then
    seven_rem=$(fmt_remaining "$seven_resets")
    printf " ${GRAY}|${RESET} ${GRAY}7d${RESET} ${seven_color}%d%%${RESET} ${GRAY}(%s)${RESET}" "$seven_int" "$seven_rem"
  else
    printf " ${GRAY}|${RESET} ${GRAY}7d${RESET} ${seven_color}%d%%${RESET}" "$seven_int"
  fi
else
  printf " ${GRAY}|${RESET} ${GRAY}7d --${RESET}"
fi

printf "${RESET}\n"
