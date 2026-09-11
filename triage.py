#!/usr/bin/env python3
"""Local health triage for the drop monitor.

Answers one question for an unattended caller: is anything wrong, and what.
Prints a verdict line starting with OK, ATTENTION, or CRITICAL, then detail.
Exit code: 0 = fine, 1 = needs attention, 2 = the monitor is not running.

Reads the CLOUD state (origin/main), not the working tree, because the cloud
is the source of truth and a local checkout can be days stale.

Run:  python triage.py
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

REPO = r"C:\dev\typ3-monitor"

# The loop polls every 60s. Allow generous slack for a handoff between loops
# (~15s) plus GitHub being slow, before calling the monitor dead.
DEAD_AFTER_MIN = 25
# A site is worth mentioning only once it has been failing for hours.
# Beleafer's Cloudflare block comes and goes in minutes; paging on that is
# what made the alerting useless in the first place.
SITE_ATTENTION_MIN = 120
# Per-site staleness catches the failure mode that raises no exception at all:
# a site that quietly stops updating while everything reports healthy.
SITE_STALE_MIN = 180


def git(*args):
    return subprocess.run(("git", "-C", REPO) + args, capture_output=True,
                          text=True, timeout=120)


def age_min(iso, now):
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 60.0
    except ValueError:
        return None


def evaluate():
    """Return (worst, problems, notes). worst is OK / ATTENTION / CRITICAL.

    Split out from main() so an unattended fixer can branch on the result
    without scraping printed text.
    """
    if git("fetch", "origin", "--quiet").returncode != 0:
        return "ATTENTION", [("ATTENTION", "cannot reach the GitHub remote; "
                              "triage inconclusive")], []

    shown = git("show", "origin/main:state.json")
    if shown.returncode != 0:
        return "CRITICAL", [("CRITICAL", "cannot read state.json from "
                             "origin/main")], []
    state = json.loads(shown.stdout)

    cfg = json.load(open(REPO + r"\config.json", encoding="utf-8"))
    enabled = {k for k, v in cfg.items() if isinstance(v, dict) and v.get("enabled")}

    now = datetime.now(timezone.utc)
    problems, notes = [], []

    run_age = age_min(state.get("last_run"), now)
    if run_age is None:
        problems.append(("CRITICAL", "state.json has no readable last_run"))
    elif run_age > DEAD_AFTER_MIN:
        problems.append(("CRITICAL",
                         "monitor has not polled in %.0f minutes (last run %s)"
                         % (run_age, (state.get("last_run") or "")[:19])))
    else:
        notes.append("last poll %.1f min ago" % run_age)

    health = state.get("health", {})
    if health.get("unhealthy"):
        problems.append(("CRITICAL", "alert email delivery is FAILING - a real "
                                     "drop would not reach the inbox"))

    failures = health.get("site_failures", {})
    for name in sorted(failures):
        if name not in enabled:
            continue
        entry = failures[name]
        since = age_min(entry.get("first_failed_at"), now)
        # Per-site override: a store known to flap should not be escalated on
        # the same clock as a store that has genuinely gone away. Beleafer's
        # Cloudflare block on GitHub's IPs comes and goes over hours and is
        # not fixable from our side, so escalating it at 2h is crying wolf.
        window = float(cfg.get(name, {}).get("site_degraded_after_minutes",
                                             SITE_ATTENTION_MIN))
        if since is not None and since >= window:
            problems.append(("ATTENTION", "%s failing for %.1f h (window %.0fh): %s"
                             % (name, since / 60.0, window / 60.0,
                                entry.get("last_error", "")[:160])))
        else:
            notes.append("%s failing %s min (transient, not escalated)"
                         % (name, "%.0f" % since if since is not None else "?"))

    for name in sorted(enabled):
        site = state.get("sites", {}).get(name)
        if site is None:
            problems.append(("ATTENTION", "%s is enabled but absent from state" % name))
            continue
        stale = age_min(site.get("last_polled_at") or site.get("last_seen"), now)
        # Skip sites already reported as failing: staleness is the same event.
        if stale is not None and stale >= SITE_STALE_MIN and name not in failures:
            problems.append(("ATTENTION",
                             "%s has not updated in %.1f h despite no recorded "
                             "error - possible silent failure" % (name, stale / 60.0)))

    if not problems:
        return "OK", [], notes
    worst = "CRITICAL" if any(p[0] == "CRITICAL" for p in problems) else "ATTENTION"
    return worst, problems, notes


MONITOR_DOWN = "has not polled in"


def main():
    worst, problems, notes = evaluate()
    if worst == "OK":
        print("OK: monitor healthy. " + "; ".join(notes))
        return 0
    print("%s: %d issue(s)" % (worst, len(problems)))
    for level, msg in problems:
        print("  [%s] %s" % (level, msg))
    if notes:
        print("  context: " + "; ".join(notes))
    return 2 if worst == "CRITICAL" else 1


if __name__ == "__main__":
    sys.exit(main())
