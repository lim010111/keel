#!/bin/bash
# WSL-only: playback shells out to powershell.exe via wslpath. No-op cleanly on
# any other platform (consume stdin, ack). See README "sound_*.sh".
command -v wslpath >/dev/null 2>&1 || { cat >/dev/null; echo "{}"; exit 0; }
payload="$(cat)"
# Journal (harness-journal#01): fire = permission sound played. Backgrounded and
# fail-open (`|| true`) so a journal failure never delays or breaks the sound.
# The non-WSL exit above journals nothing — the hook does nothing there either.
{ printf '%s' "$payload" | python3 "$HOME/.claude/scripts/hook_journal.py" \
    sound_permission fire played "session_id,cwd,tool_name" >/dev/null 2>&1 || true; } &
WINPATH="$(wslpath -w "$HOME/new_quest.mp3")"
powershell.exe -NoProfile -Sta -NonInteractive -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationCore; \$p = New-Object System.Windows.Media.MediaPlayer; \$p.Open([uri]'$WINPATH'); \$n=0; while(-not \$p.NaturalDuration.HasTimeSpan -and \$n -lt 40){ Start-Sleep -Milliseconds 50; \$n++ }; if(\$p.NaturalDuration.HasTimeSpan){ \$ms=\$p.NaturalDuration.TimeSpan.TotalMilliseconds } else { \$ms=3000 }; \$p.Play(); Start-Sleep -Milliseconds (\$ms+300); \$p.Close()" > /dev/null 2>&1 &
echo "{}"
