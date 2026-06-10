#!/bin/bash
# Stop hook: play a *question* sound when the turn ended by asking the user something
# (a free-text question awaiting a reply — the common case in /grill-me, /grill-with-docs),
# else the *complete* sound. Classification lives in classify_sound.py (which may consult
# a one-shot `claude -p` for genuinely ambiguous tails); see test_classify_sound.py.
#
# No-op inside a merge-gate produce subprocess (#31 seed finding) OR inside our own
# classify child: both load settings, so their Stop hook would re-fire here. Still emit
# the JSON ack so the hook response stays valid.
[ "$MERGE_GATE_PRODUCER_RUNNING" = "1" ] && { echo "{}"; exit 0; }
[ "$SOUND_CLASSIFY_RUNNING" = "1" ] && { echo "{}"; exit 0; }

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DBG="$DIR/.sound-debug.log"
payload="$(cat)"   # the Stop payload JSON (carries transcript_path); must read before exit

# Keep the debug log bounded — it gets one line per turn-end, so it grows without limit.
[ -f "$DBG" ] && [ "$(wc -l < "$DBG" 2>/dev/null || echo 0)" -gt 500 ] && \
  { tail -n 200 "$DBG" > "$DBG.tmp" 2>/dev/null && mv "$DBG.tmp" "$DBG"; }

# Classify in the FOREGROUND so the decision (and any ~5s LLM fallback on an ambiguous
# tail) is guaranteed to finish within the hook. The fast deterministic path is instant;
# only genuinely ambiguous tails pay the LLM latency. The session.json `timeout` caps it.
result="$(printf '%s' "$payload" | python3 "$DIR/classify_sound.py" 2>>"$DBG")"
IFS=$'\t' read -r decision reason tail60 <<<"$result"
[ -n "$decision" ] || decision="complete"   # fail-safe: classifier crashed -> complete

case "$decision" in
  question) MP3="$HOME/new_quest.mp3" ;;
  *)        MP3="$HOME/quest_completed.mp3" ;;
esac

# Background ONLY the audio so playback never blocks the session (as the original did).
WINPATH="$(wslpath -w "$MP3")"
powershell.exe -NoProfile -Sta -NonInteractive -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationCore; \$p = New-Object System.Windows.Media.MediaPlayer; \$p.Open([uri]'$WINPATH'); \$n=0; while(-not \$p.NaturalDuration.HasTimeSpan -and \$n -lt 40){ Start-Sleep -Milliseconds 50; \$n++ }; if(\$p.NaturalDuration.HasTimeSpan){ \$ms=\$p.NaturalDuration.TimeSpan.TotalMilliseconds } else { \$ms=3000 }; \$p.Play(); Start-Sleep -Milliseconds (\$ms+300); \$p.Close()" > /dev/null 2>&1 &

printf '%s decide=%-20s %-12s tail="%s"\n' "$(date +%H:%M:%S)" "$(basename "$MP3")" "$reason" "$tail60" >> "$DBG"
echo "{}"
