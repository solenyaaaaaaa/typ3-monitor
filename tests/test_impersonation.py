import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


class _HTTPError(Exception):
    def __init__(self, *args, response=None):
        super().__init__(*args)
        self.response = response


# The monitor declares requests in requirements.txt. The isolated test runtime
# does not bundle it, so provide only the tiny import-time surface we exercise.
if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(
        Session=lambda: object(), get=lambda *args, **kwargs: None)
if not hasattr(sys.modules["requests"], "HTTPError"):
    sys.modules["requests"].HTTPError = _HTTPError
import monitor  # noqa: E402


class FakeResponse:
    def __init__(self, status, text="", target=None):
        self.status_code = status
        self.text = text
        self.target = target
        self.encoding = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise monitor.requests.HTTPError("%s" % self.status_code, response=self)


class FakeBrowser:
    """Stands in for curl_cffi: answers per fingerprint, records the order."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, headers=None, timeout=None, impersonate=None):
        self.calls.append(impersonate)
        answer = self.answers.get(impersonate, 403)
        if isinstance(answer, Exception):
            raise answer
        status, text = answer if isinstance(answer, tuple) else (answer, "<html>ok</html>")
        return FakeResponse(status, text, impersonate)


class FakeSession:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        return FakeResponse(self.status)


class ImpersonationTests(unittest.TestCase):
    def setUp(self):
        monitor._IMPERSONATION_THAT_WORKED.clear()

    def get(self, browser, session=None, **kw):
        with patch.object(monitor, "_impersonating_get", browser), \
             patch.object(monitor, "BROWSER_IMPERSONATE", ("chrome", "safari", "firefox")):
            return monitor.http_get(session or FakeSession(403), "https://shop.example/x/",
                                    "ua", 5, **kw)

    def test_refused_fingerprint_falls_through_to_the_next(self):
        # Beleafer 2026-10-13: old Chrome refused, other browsers let through.
        browser = FakeBrowser({"chrome": 403, "safari": 200})
        resp = self.get(browser, impersonate=True)
        self.assertEqual((resp.status_code, resp.target), (200, "safari"))
        self.assertEqual(browser.calls, ["chrome", "safari"])

    def test_the_fingerprint_that_worked_leads_next_time(self):
        browser = FakeBrowser({"chrome": 403, "safari": 200})
        self.get(browser, impersonate=True)
        browser.calls.clear()
        self.get(browser, impersonate=True)
        self.assertEqual(browser.calls, ["safari"])

    def test_impersonate_true_never_sends_the_plain_request(self):
        session = FakeSession(200)
        self.get(FakeBrowser({"chrome": 200}), session=session, impersonate=True)
        self.assertEqual(session.calls, 0)

    def test_a_404_is_final_so_pagination_still_ends(self):
        browser = FakeBrowser({"chrome": 404, "safari": 200})
        with self.assertRaises(monitor.requests.HTTPError) as caught:
            self.get(browser, impersonate=True)
        self.assertEqual(caught.exception.response.status_code, 404)
        self.assertEqual(browser.calls, ["chrome"])

    def test_every_fingerprint_refused_raises_http_error(self):
        browser = FakeBrowser({})
        with self.assertRaises(monitor.requests.HTTPError):
            self.get(browser, impersonate=True)
        self.assertEqual(browser.calls, ["chrome", "safari", "firefox"])

    def test_an_unusable_fingerprint_is_skipped(self):
        browser = FakeBrowser({"chrome": RuntimeError("unknown target"), "safari": 200})
        self.assertEqual(self.get(browser, impersonate=True).target, "safari")

    def test_plain_403_still_falls_back_for_ordinary_sites(self):
        session = FakeSession(403)
        resp = self.get(FakeBrowser({"chrome": 200}), session=session)
        self.assertEqual((session.calls, resp.target), (1, "chrome"))

    def test_missing_expected_content_tries_the_next_fingerprint(self):
        pattern = re.compile("product-list")
        browser = FakeBrowser({"chrome": (200, "<html>challenge</html>"),
                               "safari": (200, "<html>product-list</html>")})
        resp = self.get(browser, impersonate=True, expect_pattern=pattern)
        self.assertEqual(resp.target, "safari")


if __name__ == "__main__":
    unittest.main()
