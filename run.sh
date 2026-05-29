#!/usr/bin/env bash
# Linux runner for the drop monitor (used by cron on the always-on VM).
# Loads secrets from ~/dropmon.env (NOT in the repo), then runs one poll.
set -uo pipefail

cd "$(dirname "$0")"

# Load secrets / config that must not live in the public repo:
#   SMTP_USER, SMTP_PASSWORD, EMAIL_TO, HEALTHCHECK_URL
set -a
if [ -f "$HOME/dropmon.env" ]; then
  . "$HOME/dropmon.env"
fi
set +a

# Run one poll. All output appended to a local log (rotated by logrotate / size check).
python3 monitor.py >> "$HOME/dropmon.log" 2>&1

# Keep the log from growing forever: trim to last 5000 lines.
if [ -f "$HOME/dropmon.log" ]; then
  tail -n 5000 "$HOME/dropmon.log" > "$HOME/dropmon.log.tmp" 2>/dev/null && mv "$HOME/dropmon.log.tmp" "$HOME/dropmon.log"
fi
