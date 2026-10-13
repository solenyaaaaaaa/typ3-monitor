import os
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

    def __init__(self, mode="fail", min_interval=0):
        self.mode = mode
        self.cfg = {"min_interval_minutes": min_interval}
        self.fetch_calls = 0

    def fetch(self, session, ua, timeout):
        self.fetch_calls += 1
        if self.mode == "fail":
            raise RuntimeError("temporary upstream error")
        return {"ok": True}

    def diff(self, previous, current):
        if self.mode == "diff-fail":
            raise RuntimeError("bad snapshot")
        if self.mode == "alert":
            return {"catalog": "new"}, [{
                "site": self.name, "label": "Example", "kind": "DROP",
                "title": "Test", "url": "https://example.invalid", "details": "",
            }]
        return {"catalog": "new"}, []


class HealthReportingTests(unittest.TestCase):
    def run_main(self, state, site, *, email_enabled=False, email_result=True):
        pings = []
        cfg = {
            "user_agent": "test", "timeout_sec": 1,
            "email_enabled": email_enabled, "site_failure_threshold": 3,
            "site_degraded_after_minutes": 120,
        }
        with patch.object(monitor, "setup_logging"), \
             patch.object(monitor, "load_config", return_value=cfg), \
             patch.object(monitor, "build_sites", return_value=[site]), \
             patch.object(monitor, "load_state", return_value=state), \
             patch.object(monitor, "save_state"), \
             patch.object(monitor, "desktop_alerts"), \
             patch.object(monitor, "email_alerts", return_value=email_result), \
             patch.object(monitor, "ping_healthcheck", side_effect=lambda success, body=None: pings.append(success) or True):
            result = monitor.main()
        return result, pings

    def test_persistent_failure_is_recorded_not_signalled_then_success_clears_it(self):
        state = {"sites": {"example": {"catalog": "old"}}}
        site = FakeSite("fail")

        # Only undelivered alert email may put the check down. A failing store
        # is counted (capped at the threshold) for triage to read, while every
        # run still sends the normal heartbeat and exits 0.
        for expected_count in (1, 2, 3, 3):
            result, pings = self.run_main(state, site)
            self.assertEqual(result, 0)
            self.assertEqual(pings, [True])
            self.assertFalse(state["health"]["unhealthy"])
            failure = state["health"]["site_failures"]["example"]
            self.assertEqual(failure["count"], expected_count)
            self.assertEqual(failure["last_error"], "temporary upstream error")

        site.mode = "success"
        result, pings = self.run_main(state, site)
        self.assertEqual(result, 0)
        self.assertEqual(pings, [True])
        self.assertNotIn("example", state["health"]["site_failures"])
        self.assertFalse(state["health"]["unhealthy"])

    def test_site_is_degraded_only_after_continuous_failure_for_the_window(self):
        state = {"sites": {"example": {"catalog": "old"}}}
        site = FakeSite("fail")

        def failing_since(minutes):
            state["health"]["site_failures"]["example"]["first_failed_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=minutes)
            ).isoformat()

        # The capped count cannot tell three minutes from three days, so a
        # streak at the threshold is not degraded until it has lasted
        # site_degraded_after_minutes.
        for _ in range(3):
            self.run_main(state, site)
        self.assertEqual(state["health"]["site_failures"]["example"]["count"], 3)
        self.assertEqual(state["health"]["degraded_sites"], [])

        failing_since(119)
        self.run_main(state, site)
        self.assertEqual(state["health"]["degraded_sites"], [])

        # The next failure keeps the streak's first_failed_at, which is now
        # past the window. Degraded is for triage; the check stays up.
        failing_since(121)
        result, pings = self.run_main(state, site)
        self.assertEqual(state["health"]["degraded_sites"], ["example"])
        self.assertEqual(result, 0)
        self.assertEqual(pings, [True])
        self.assertFalse(state["health"]["unhealthy"])

        site.mode = "success"
        self.run_main(state, site)
        self.assertEqual(state["health"]["degraded_sites"], [])

    def test_interval_skip_does_not_change_failure_health(self):
        state = {"sites": {"example": {
            "last_polled_at": datetime.now(timezone.utc).isoformat(),
        }}}
        site = FakeSite("fail", min_interval=5)
        result, pings = self.run_main(state, site)
        self.assertEqual(result, 0)
        self.assertEqual(pings, [True])
        self.assertEqual(site.fetch_calls, 0)
        self.assertEqual(state["health"]["site_failures"], {})

    def test_interval_skip_preserves_an_existing_failure_record(self):
        failure = {
            "count": 3,
            "first_failed_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
            "last_error": "prior failure",
        }
        state = {"sites": {"example": {
            "last_polled_at": datetime.now(timezone.utc).isoformat(),
        }}, "health": {"site_failures": {"example": dict(failure)}}}
        site = FakeSite("success", min_interval=5)
        result, pings = self.run_main(state, site)
        self.assertEqual(result, 0)
        self.assertEqual(pings, [True])
        self.assertEqual(site.fetch_calls, 0)
        # A skip is not a successful poll: the streak is neither cleared nor
        # extended, so a site that was degraded stays degraded.
        self.assertEqual(state["health"]["site_failures"]["example"], failure)
        self.assertEqual(state["health"]["degraded_sites"], ["example"])

    def test_diff_failures_use_the_same_persistent_threshold(self):
        state = {"sites": {"example": {"catalog": "old"}}}
        site = FakeSite("diff-fail")
        for expected_count in (1, 2, 3, 3):
            result, pings = self.run_main(state, site)
            self.assertEqual(result, 0)
            self.assertEqual(pings, [True])
            self.assertEqual(state["health"]["site_failures"]["example"]["count"], expected_count)
        self.assertEqual(state["health"]["site_failures"]["example"]["last_error"], "bad snapshot")
        self.assertFalse(state["health"]["unhealthy"])

    def test_email_delivery_failure_is_immediately_unhealthy(self):
        state = {"sites": {"example": {"catalog": "old"}}}
        result, pings = self.run_main(
            state, FakeSite("alert"), email_enabled=True, email_result=False
        )
        self.assertEqual(result, 1)
        self.assertEqual(pings, [False])
        self.assertTrue(state["health"]["unhealthy"])

    def test_healthcheck_http_error_is_reported_as_failure(self):
        class BadResponse:
            def raise_for_status(self):
                raise RuntimeError(secret_url)

        secret_url = "https://health.invalid/secret-token"
        with patch.dict(os.environ, {"HEALTHCHECK_URL": secret_url}, clear=False), \
             patch.object(monitor.requests, "get", return_value=BadResponse()), \
             self.assertLogs(level="WARNING") as captured:
            self.assertFalse(monitor.ping_healthcheck())
        self.assertNotIn(secret_url, "\n".join(captured.output))

    def test_invalid_configured_healthcheck_url_fails_without_echoing_it(self):
        bad_url = "ftp://health.invalid/secret-token"
        with patch.dict(os.environ, {"HEALTHCHECK_URL": bad_url}, clear=False), \
             self.assertLogs(level="WARNING") as captured:
            self.assertFalse(monitor.ping_healthcheck())
        self.assertNotIn(bad_url, "\n".join(captured.output))

    def test_disabled_site_counter_is_not_left_unhealthy(self):
        state = {"sites": {}, "health": {"site_failures": {
            "disabled": {"count": 3, "last_error": "old"},
        }}}
        result, pings = self.run_main(state, FakeSite("success"))
        self.assertEqual(result, 0)
        self.assertEqual(pings, [True])
        self.assertNotIn("disabled", state["health"]["site_failures"])


if __name__ == "__main__":
    unittest.main()
