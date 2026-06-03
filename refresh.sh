#!/bin/bash
# Refresh for the Ablo Studio Marketing OS (MANUAL-RUN copy).
# Regenerates data.js from content.json + live sources, then commits and pushes
# so GitHub Pages redeploys.
#
# NOTE: launchd does NOT run this file. macOS TCC blocks launchd from executing
# scripts inside ~/Documents (it failed with exit 126 "Operation not permitted").
# The unattended/launchd entrypoint is ~/.local/bin/ablo-marketing-os-refresh.sh
# (outside the TCC-protected folder). Keep the two in sync. Run this one by hand
# from a Terminal that already has Documents access.
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
  git commit -m "chore: daily data refresh $(date '+%Y-%m-%d')" >> "$LOG" 2>&1
  if git push origin main >> "$LOG" 2>&1; then
    echo "pushed OK" >> "$LOG"
  else
    echo "push failed (network/auth?) -- will retry next run" >> "$LOG"
  fi
else
  echo "no changes" >> "$LOG"
fi
echo "" >> "$LOG"
