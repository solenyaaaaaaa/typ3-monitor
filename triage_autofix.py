#!/usr/bin/env python3
"""Unattended watchdog for the drop monitor. No Claude, no prompts, no mail.

Runs from Windows Task Scheduler. Checks the monitor, restarts it if it has
stopped polling, and stays completely silent when everything is fine.

Why this is a plain script and not a Claude scheduled task: a Claude session
asks for tool approvals by design, so using one for unattended babysitting
produced a permission prompt every few hours - the exact opposite of leaving
the monitor alone. Restarting a dead loop is pure mechanics and needs no
judgement, so it does not need a model.

What it will NOT do: rewrite scraper code. A shop that restructures needs a
decision about what should now be watched, and a wrong guess yields silent
wrong monitoring, which is worse than a known outage. Those cases are written
to MONITOR-NEEDS-ATTENTION.md instead.

Exit codes: 0 healthy (or fixed), 1 needs a human, 2 could not fix.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, r"C:\dev\typ3-monitor")
import triage  # noqa: E402

REPO = r"C:\dev\typ3-monitor"
API = "https://api.github.com/repos/solenyaaaaaaa/typ3-monitor"
LOG = r"C:\Users\me\.claude\scripts\drop-monitor-triage.log"
ATTENTION = (r"C:\Users\me\OneDrive\Desktop\claude code folder"
             r"\MONITOR-NEEDS-ATTENTION.md")
HTTP_TIMEOUT = 30


def log(line):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (stamp, line))
    except OSError:
        pass
    print(line)


def github_token():
    """Reuse the Git Credential Manager token; `gh` is not installed here."""
    try:
        out = subprocess.run(["git", "-C", REPO, "credential", "fill"],
                             input="protocol=https\nhost=github.com\n\n",
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None


def api(session, method, path, token, **kw):
    return session.request(method, API + path, timeout=HTTP_TIMEOUT,
                           headers={"Authorization": "Bearer " + token,
                                    "Accept": "application/vnd.github+json"},
                           **kw)


def restart_loop(token):
    """Restart the poll loop. Returns a short description of what was done."""
    import requests
    s = requests.Session()
    try:
        r = api(s, "GET", "/actions/runs?status=in_progress&per_page=1", token)
        runs = r.json().get("workflow_runs", []) if r.ok else []
    except Exception as exc:
        return "could not query runs: %s" % exc

    if runs:
        run_id = runs[0]["id"]
        try:
            api(s, "POST", "/actions/runs/%s/cancel" % run_id, token)
        except Exception as exc:
            return "cancel failed: %s" % exc
        # Wait for the cancel to actually land. Dispatching while a run is
        # still cancelling is the race that left this repo with permanently
        # stuck runs (84294, 90164, 143238, 143301).
        for _ in range(18):
            time.sleep(10)
            try:
                st = api(s, "GET", "/actions/runs/%s" % run_id, token).json()
            except Exception:
                continue
            if st.get("status") == "completed":
                break
        else:
            return "wedged run %s would not reach a terminal state" % run_id

    try:
        d = api(s, "POST", "/actions/workflows/poll.yml/dispatches", token,
                json={"ref": "main"})
    except Exception as exc:
        return "dispatch failed: %s" % exc
    if d.status_code != 204:
        return "dispatch returned HTTP %s" % d.status_code
    return "restarted the poll loop" + (" after cancelling a wedged run"
                                        if runs else "")


def write_attention(worst, problems, action):
    body = ["# Drop monitor needs attention", "",
            "Written by triage_autofix.py at %s."
            % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "",
            "Status: %s" % worst, ""]
    for level, msg in problems:
        body.append("- [%s] %s" % (level, msg))
    body += ["", "Automatic action taken: %s" % (action or "none"), "",
             "This file is deleted automatically once triage comes back clean.",
             "Ask Claude to look at the drop monitor when convenient."]
    try:
        with open(ATTENTION, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body) + "\n")
    except OSError as exc:
        log("could not write attention file: %s" % exc)


def clear_attention():
    import os
    try:
        if os.path.exists(ATTENTION):
            os.remove(ATTENTION)
            return True
    except OSError:
        pass
    return False


def main():
    worst, problems, notes = triage.evaluate()

    if worst == "OK":
        if clear_attention():
            log("OK - recovered; cleared MONITOR-NEEDS-ATTENTION.md")
        else:
            log("OK - %s" % ("; ".join(notes) or "healthy"))
        return 0

    monitor_down = any(triage.MONITOR_DOWN in msg or "cannot read state.json" in msg
                       for _, msg in problems)
    action = None

    if monitor_down:
        token = github_token()
        if not token:
            action = "no GitHub token available; could not restart"
        else:
            action = restart_loop(token)
            log("monitor down -> %s" % action)
            time.sleep(180)
            worst, problems, notes = triage.evaluate()
            if worst == "OK":
                clear_attention()
                log("OK - fixed automatically (%s)" % action)
                return 0

    log("%s - %s" % (worst, "; ".join(m for _, m in problems)))
    write_attention(worst, problems, action)
    return 2 if worst == "CRITICAL" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let the watchdog itself die noisily
        log("watchdog error: %r" % exc)
        sys.exit(2)
