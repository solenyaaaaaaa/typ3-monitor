#!/usr/bin/env bash
# Poll loop for the drop monitor.
#
# Replaces the old one-run-per-poll model, where an external scheduler
# (cron-job.org, then Google Cloud Scheduler) had to dispatch a fresh workflow
# run every 60 seconds. Both of those schedulers eventually went away silently
# and took the monitor with them. This keeps a single job alive and does the
# polling itself, so the only thing that has to keep working is GitHub.
#
# Keeps polling through individual failures so healthy sites retain coverage.
# Its final status still reports an active persistent monitor/state failure.
set -uo pipefail

INTERVAL="${POLL_INTERVAL:-60}"
DURATION="${LOOP_SECONDS:-19800}"   # 5h30m, inside the 6h GitHub job ceiling
END=$(( $(date +%s) + DURATION ))

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

push_state() {
  # Same retry-with-rebase strategy the per-run workflow used: absorbs git
  # server 5xx and any race with a manual push. Only one loop runs at a time
  # (see the workflow's concurrency group), so conflicts are rare.
  if ! git add state.json; then
    echo "::error::could not stage state.json"
    return 1
  fi
  # A previous iteration may have committed successfully but lost its push.
  # Only create a new commit when state.json is staged; otherwise retry that
  # pending local commit instead of treating "nothing to commit" as success.
  if ! git diff --cached --quiet -- state.json; then
    if ! git commit -q -m "state: update [skip ci]"; then
      echo "::error::could not commit state.json"
      return 1
    fi
  fi
  for attempt in 1 2 3 4; do
    if timeout 90 git push -q 2>&1; then
      return 0
    fi
    echo "push attempt $attempt failed; rebasing and retrying..."
    if ! timeout 90 git fetch -q origin main; then
      echo "fetch attempt $attempt failed"
      sleep $(( attempt * 3 ))
      continue
    fi
    if ! git rebase -q origin/main; then
      echo "rebase conflict, aborting"
      git rebase --abort
    fi
    sleep $(( attempt * 3 ))
  done
  echo "::error::could not push state.json after 4 attempts"
  return 1
}

iteration=0
polls_ok=0
polls_failed=0
commits=0
polls_timed_out=0
last_poll_status=0
state_push_failed=0

echo "loop starting: ${DURATION}s at ${INTERVAL}s intervals"

while [ "$(date +%s)" -lt "$END" ]; do
  iteration=$(( iteration + 1 ))
  started=$(date +%s)

  # Bound every poll. One hung fetch must never stall the loop: on 2026-09-10
  # run 157443 sat 17 minutes on a single iteration and committed nothing, so
  # the monitor was dark while the job still looked alive and healthy. Losing
  # one iteration is always cheaper than losing the loop. A full local run is
  # ~21s, so 150s is generous headroom, not a tight bound.
  if timeout --kill-after=15s "${POLL_TIMEOUT:-150}" python monitor.py; then
    polls_ok=$(( polls_ok + 1 ))
    last_poll_status=0
  else
    rc=$?
    polls_failed=$(( polls_failed + 1 ))
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
      polls_timed_out=$(( polls_timed_out + 1 ))
      echo "::warning::poll TIMED OUT after ${POLL_TIMEOUT:-150}s on iteration ${iteration}"
    else
      echo "::warning::monitor.py exited ${rc} on iteration ${iteration}"
    fi
    last_poll_status=1
  fi

  if ! git diff --quiet -- state.json || [ "$state_push_failed" -ne 0 ]; then
    if push_state; then
      commits=$(( commits + 1 ))
      state_push_failed=0
    else
      state_push_failed=1
    fi
  fi

  # Hold the cadence at INTERVAL regardless of how long the poll took, so a
  # slow site does not stretch the polling interval.
  elapsed=$(( $(date +%s) - started ))
  remaining=$(( INTERVAL - elapsed ))
  if [ "$remaining" -gt 0 ]; then
    # Do not sleep past the end of the window.
    left=$(( END - $(date +%s) ))
    [ "$left" -lt "$remaining" ] && remaining="$left"
    [ "$remaining" -gt 0 ] && sleep "$remaining"
  fi
done

echo "loop finished: ${iteration} iterations, ${polls_ok} ok, ${polls_failed} failed (${polls_timed_out} timed out), ${commits} state commits"
if [ "$last_poll_status" -ne 0 ] || [ "$state_push_failed" -ne 0 ]; then
  exit 1
fi
exit 0
