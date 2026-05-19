#!/bin/bash
cat > /dev/null
powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -Command "(New-Object Media.SoundPlayer 'C:\Windows\Media\Windows Notify Email.wav').PlaySync()" > /dev/null 2>&1 &
echo "{}"
