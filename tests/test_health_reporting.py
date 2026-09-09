import os
import sys
import types
import unittest
from datetime import datetime, timezone
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
        }
        with patch.object(monitor, "setup_logging"), \
             patch.object(monitor, "load_config", return_value=cfg), \
             patch.object(monitor, "build_sites", return_value=[site]), \
             patch.object(monitor, "load_state", return_value=state), \
             patch.object(monitor, "save_state"), \
             patch.object(monitor, "desktop_alerts"), \
             patch.object(monitor, "email_alerts", return_value=email_result), \
             patch.object(monitor, "ping_healthcheck", side_effect=lambda success: pings.append(success) or True):
            result = monitor.main()
        return result, pings

    def test_persistent_failure_signals_then_success_clears_it(self):
        state = {"sites": {"example": {"catalog": "old"}}}
        site = FakeSite("fail")

        for expected_count in (1, 2):
            result, pings = self.run_main(state, site)
            self.assertEqual(result, 0)
            self.assertEqual(pings, [True])
            self.assertEqual(state["health"]["site_failures"]["example"]["count"], expected_count)

        result, pings = self.run_main(state, site)
        self.assertEqual(result, 1)
        self.assertEqual(pings, [False])
        self.assertTrue(state["health"]["unhealthy"])

        # Persistent failures keep emitting /fail; the provider deduplicates
        # down/up notifications and this prevents a lost first signal.
        result, pings = self.run_main(state, site)
        self.assertEqual(result, 1)
        self.assertEqual(pings, [False])
        self.assertEqual(state["health"]["site_failures"]["example"]["count"], 3)

        site.mode = "success"
        result, pings = self.run_main(state, site)
        self.assertEqual(result, 0)
        self.assertEqual(pings, [True])
        self.assertNotIn("example", state["health"]["site_failures"])
        self.assertFalse(state["health"]["unhealthy"])

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

    def test_interval_skip_preserves_an_existing_unhealthy_counter(self):
        state = {"sites": {"example": {
            "last_polled_at": datetime.now(timezone.utc).isoformat(),
        }}, "health": {"site_failures": {
            "example": {"count": 3, "last_error": "prior failure"},
        }}}
        site = FakeSite("success", min_interval=5)
        result, pings = self.run_main(state, site)
        self.assertEqual(result, 1)
        self.assertEqual(pings, [False])
        self.assertEqual(site.fetch_calls, 0)
        self.assertEqual(state["health"]["site_failures"]["example"]["count"], 3)

    def test_diff_failures_use_the_same_persistent_threshold(self):
        state = {"sites": {"example": {"catalog": "old"}}}
        site = FakeSite("diff-fail")
        for expected_result in (0, 0, 1):
            result, _ = self.run_main(state, site)
            self.assertEqual(result, expected_result)
        self.assertEqual(state["health"]["site_failures"]["example"]["count"], 3)

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
