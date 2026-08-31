#!/usr/bin/env bash
# Start the next poll loop as this one exits, so coverage is continuous.
#
# GITHUB_TOKEN normally cannot trigger workflows (GitHub blocks that to prevent
# runaway recursion), but workflow_dispatch and repository_dispatch are the
# documented exceptions, which is why this can hand off without a PAT.
#
# If that ever stops working, the workflow's `schedule` trigger is the safety
# net: it restarts the loop on its own. Slower to recover, but it needs no
# credential at all, so the two mechanisms cannot fail for the same reason.
set -uo pipefail

API="https://api.github.com/repos/${GITHUB_REPOSITORY}/actions/workflows/poll.yml/dispatches"

for attempt in 1 2 3; do
  code=$(curl -sS -o /tmp/handoff_body.txt -w '%{http_code}' -X POST \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$API" \
    -d '{"ref":"main"}')

  if [ "$code" = "204" ]; then
    echo "handoff accepted (attempt ${attempt}); next loop dispatched"
    exit 0
  fi

  echo "handoff attempt ${attempt} returned HTTP ${code}: $(cat /tmp/handoff_body.txt)"
  sleep $(( attempt * 5 ))
done

# Do not fail the job over this. A failed handoff costs coverage until the
# scheduled watchdog fires; failing the run would additionally spam GitHub
# failure email for something the watchdog already handles.
echo "::warning::handoff failed after 3 attempts; waiting on the scheduled watchdog to restart the loop"
exit 0
