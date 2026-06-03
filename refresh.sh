#!/bin/bash
# Weekly refresh for the Ablo Studio Marketing OS.
# Regenerates data.js from content.json + live sources, then commits and pushes
# so GitHub Pages redeploys. Run by launchd (com.alejo.ablo-marketing-os.weekly).
set -uo pipefail

DIR="/Users/alejo/Documents/Claude/ablo-marketing-os"
cd "$DIR" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
LOG="$DIR/.refresh.log"

echo "===== refresh $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$LOG"

python3 build.py >> "$LOG" 2>&1
if [[ $? -ne 0 ]]; then
  echo "build.py FAILED, leaving last good data.js in place" >> "$LOG"
  exit 1
fi

if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  git add -A >> "$LOG" 2>&1
  git commit -m "chore: weekly data refresh $(date '+%Y-%m-%d')" >> "$LOG" 2>&1
  if git push origin main >> "$LOG" 2>&1; then
    echo "pushed OK" >> "$LOG"
  else
    echo "push failed (network/auth?) -- will retry next week" >> "$LOG"
  fi
else
  echo "no changes" >> "$LOG"
fi
echo "" >> "$LOG"
