#!/bin/bash
powershell.exe -NoProfile -Sta -NonInteractive -WindowStyle Hidden -Command "(New-Object Media.SoundPlayer 'C:\Windows\Media\Windows Notify Calendar.wav').PlaySync()" > /dev/null 2>&1 &
echo "{}"
