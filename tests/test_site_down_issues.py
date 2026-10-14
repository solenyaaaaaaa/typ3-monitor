import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
# The monitor declares requests in requirements.txt. The isolated test runtime
# does not bundle it, so provide only the tiny import-time surface we exercise.
if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(
        Session=lambda: object(), get=lambda *args, **kwargs: None
    )
import monitor  # noqa: E402


class FakeSite:
    name = "example"
    label = "Example Shop"

    def __init__(self, mode="fail", cfg=None):
        self.mode = mode
        self.cfg = dict(cfg or {}, category_url="https://shop.example/indoor/")

    def fetch(self, session, ua, timeout):
        if self.mode == "fail":
            raise RuntimeError("403 Error for url: https://shop.example/indoor/")
        return {"ok": True}

    def diff(self, previous, current):
        return {"catalog": "new"}, []


class FakeGitHub:
    """Records GitHub API calls; open issues are kept like the real API."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.open_issues = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path.split("?")[0], payload))
        if self.fail:
            return None
        if method == "GET":
            return list(self.open_issues)
        if method == "POST" and path == "issues":
            issue = {"number": 41 + len(self.open_issues), "title": payload["title"]}
            self.open_issues.append(issue)
            return issue
        return {}

    def made(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


def failing_for(hours):
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return {"sites": {"example": {"catalog": "old"}}, "health": {"site_failures": {
        "example": {"count": 3, "first_failed_at": start, "last_error": "403",
                    "last_failed_at": start}}}}


class SiteDownIssueTests(unittest.TestCase):
    def run_main(self, state, site, github, hours=6):
        cfg = {"user_agent": "test", "timeout_sec": 1, "email_enabled": False,
               "site_failure_threshold": 3, "site_degraded_after_minutes": 120,
               "site_down_issue_after_hours": hours}
        with patch.object(monitor, "setup_logging"), \
             patch.object(monitor, "load_config", return_value=cfg), \
             patch.object(monitor, "build_sites", return_value=[site]), \
             patch.object(monitor, "load_state", return_value=state), \
             patch.object(monitor, "save_state"), \
             patch.object(monitor, "desktop_alerts"), \
             patch.object(monitor, "email_alerts", return_value=True), \
             patch.object(monitor, "github_api", github), \
             patch.object(monitor, "ping_healthcheck", return_value=True):
            return monitor.main()

    def test_short_outage_opens_nothing(self):
        github = FakeGitHub()
        self.run_main(failing_for(2), FakeSite("fail"), github)
        self.assertEqual(github.made("POST", "issues"), [])

    def test_one_issue_per_outage_and_it_survives_later_failures(self):
        state, github = failing_for(7), FakeGitHub()
        self.assertEqual(self.run_main(state, FakeSite("fail"), github), 0)
        created = github.made("POST", "issues")
        self.assertEqual(len(created), 1)
        payload = created[0][2]
        self.assertTrue(payload["title"].startswith("Site down: example (Example Shop)"))
        self.assertEqual(payload["labels"], ["site-down"])
        self.assertIn("403", payload["body"])
        self.assertIn("https://shop.example/indoor/", payload["body"])
        self.assertEqual(state["health"]["site_failures"]["example"]["issue"], 41)
        # More failures keep the issue on the streak and never open another.
        self.run_main(state, FakeSite("fail"), github)
        self.run_main(state, FakeSite("fail"), github)
        self.assertEqual(len(github.made("POST", "issues")), 1)
        self.assertEqual(state["health"]["site_failures"]["example"]["issue"], 41)

    def test_an_issue_already_open_is_adopted_not_duplicated(self):
        # A state push can fail after the issue was created.
        state, github = failing_for(9), FakeGitHub()
        github.open_issues.append({"number": 7, "title": "Site down: example (Example Shop) failing 6 h"})
        self.run_main(state, FakeSite("fail"), github)
        self.assertEqual(github.made("POST", "issues"), [])
        self.assertEqual(state["health"]["site_failures"]["example"]["issue"], 7)

    def test_recovery_comments_and_closes_the_issue(self):
        state, github = failing_for(8), FakeGitHub()
        self.run_main(state, FakeSite("fail"), github)
        self.run_main(state, FakeSite("ok"), github)
        self.assertNotIn("example", state["health"]["site_failures"])
        comment = github.made("POST", "issues/41/comments")
        self.assertEqual(len(comment), 1)
        self.assertIn("Recovered", comment[0][2]["body"])
        self.assertEqual(github.made("PATCH", "issues/41")[0][2]["state"], "closed")

    def test_a_failed_close_is_retried_on_later_runs(self):
        state, github = failing_for(8), FakeGitHub()
        self.run_main(state, FakeSite("fail"), github)
        github.fail = True
        self.run_main(state, FakeSite("ok"), github)
        self.assertEqual(state["health"]["pending_issue_closes"], [41])
        github.fail = False
        self.run_main(state, FakeSite("ok"), github)
        self.assertNotIn("pending_issue_closes", state["health"])
        self.assertEqual(len(github.made("PATCH", "issues/41")), 2)

    def test_github_trouble_never_fails_the_poll(self):
        state, github = failing_for(10), FakeGitHub(fail=True)
        self.assertEqual(self.run_main(state, FakeSite("fail"), github), 0)
        self.assertNotIn("issue", state["health"]["site_failures"]["example"])

    def test_a_site_can_opt_out_or_override_the_threshold(self):
        github = FakeGitHub()
        self.run_main(failing_for(8), FakeSite("fail", {"site_down_issue_after_hours": 0}), github)
        self.run_main(failing_for(8), FakeSite("fail", {"site_down_issue_after_hours": 12}), github)
        self.assertEqual(github.made("POST", "issues"), [])

    def test_without_a_token_the_api_is_never_called(self):
        with patch.dict(monitor.os.environ, {"GITHUB_TOKEN": "", "GITHUB_REPOSITORY": ""}):
            self.assertIsNone(monitor.github_api("GET", "issues"))


if __name__ == "__main__":
    unittest.main()
