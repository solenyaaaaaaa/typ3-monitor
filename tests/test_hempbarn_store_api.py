"""Hemp Barn reads the WooCommerce Store API first, the product page second.

Regression for 2026-11-19 (issues #4/#5): the product pages went behind
SiteGround's "One moment, please..." bot check and both sites were dark 6 h.
"""
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
# Same requests stub as the other tests: only the import-time surface.
if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(
        Session=lambda: object(), get=lambda *args, **kwargs: None,
        HTTPError=type("HTTPError", (Exception,), {}),
    )
import monitor  # noqa: E402

URL = "https://www.thehempbarn.com/product/livingsoil/"
API = "https://www.thehempbarn.com/wp-json/wc/store/v1/products?slug=livingsoil"

# Cut-down copy of the real short description (2026-11-19): bold "Top Tier"
# mid-sentence, a heading with genetics, and an orphan heading (sold out).
SHORT_DESC = (
    '<p><strong>ORGANIC, NO TILL LIVING SOIL GROWN</strong> for terps. Our '
    '<strong>&#8220;Top Tier&#8221;</strong> line.</p>\n'
    '<p><strong>Oakland OG (OG x Chem)</strong> One of our all time favorites.</p>\n'
    '<p><strong>G-Chem</strong> A 10/10 that has sold out.</p>\n'
    '<p><strong>King Louie</strong> Piney and loud.</p>'
)
API_JSON = [{
    "slug": "livingsoil",
    "short_description": SHORT_DESC,
    "variations": [
        {"id": 1, "attributes": [{"name": "Strain", "value": "Oakland OG"},
                                 {"name": "Size", "value": "3.5 Grams"}]},
        {"id": 2, "attributes": [{"name": "Strain", "value": "King Louie"},
                                 {"name": "Size", "value": "7 Grams"}]},
        {"id": 3, "attributes": [{"name": "Strain", "value": "Oakland OG"},
                                 {"name": "Size", "value": "28 Grams"}]},
    ],
}]
PAGE = (
    '<form data-product_variations="[{&quot;attributes&quot;:{&quot;attribute_strain'
    '&quot;:&quot;Oakland OG&quot;}},{&quot;attributes&quot;:{&quot;attribute_strain'
    '&quot;:&quot;King Louie&quot;}}]"></form>'
    '<div class="woocommerce-product-details__short-description">'
    + SHORT_DESC + '</div>'
)
CHALLENGE = "<html><title>One moment, please...</title>verifying</html>"


class Resp:
    def __init__(self, text="", data=None):
        self.text = text
        self._data = data

    def json(self):
        return self._data


def site():
    return monitor.HempBarnLivingSoilSite({"enabled": True, "url": URL})


class StoreApiTests(unittest.TestCase):
    def test_api_url_derived_from_product_url(self):
        self.assertEqual(site()._store_api_url(), API)

    def test_api_gives_same_snapshot_as_page(self):
        def fake(session, url, ua, timeout, expect_json=False, expect_pattern=None, **kw):
            if url == API:
                return Resp(data=API_JSON)
            return Resp(text=PAGE)
        with patch.object(monitor, "http_get", side_effect=fake):
            from_api = site().fetch(None, "ua", 5)
        with patch.object(monitor, "http_get", side_effect=fake):
            from_page = site()._fetch_page(None, "ua", 5)
        self.assertEqual(from_api["strains"], ["King Louie", "Oakland OG"])
        self.assertEqual(from_api, from_page)
        self.assertIn("all time", from_api["descriptions"]["Oakland OG"])
        # The orphan G-Chem heading still ends Oakland OG's chunk.
        self.assertNotIn("10/10", from_api["descriptions"]["Oakland OG"])

    def test_page_challenged_api_still_works(self):
        calls = []

        def fake(session, url, ua, timeout, expect_json=False, expect_pattern=None, **kw):
            calls.append(url)
            if url == API:
                return Resp(data=API_JSON)
            raise monitor.requests.HTTPError("403 Error for url: " + url)
        with patch.object(monitor, "http_get", side_effect=fake):
            snap = site().fetch(None, "ua", 5)
        self.assertEqual(calls, [API])  # page never requested when API works
        self.assertEqual(snap["strains"], ["King Louie", "Oakland OG"])

    def test_falls_back_to_page_when_api_fails(self):
        def fake(session, url, ua, timeout, expect_json=False, expect_pattern=None, **kw):
            if url == API:
                raise monitor.requests.HTTPError("404 Error for url: " + url)
            return Resp(text=PAGE)
        with patch.object(monitor, "http_get", side_effect=fake):
            snap = site().fetch(None, "ua", 5)
        self.assertEqual(snap["strains"], ["King Louie", "Oakland OG"])

    def test_both_failing_raises_with_both_errors(self):
        def fake(session, url, ua, timeout, expect_json=False, expect_pattern=None, **kw):
            if url == API:
                return Resp(data=[])  # unknown slug: never an empty snapshot
            return Resp(text=CHALLENGE)
        with patch.object(monitor, "http_get", side_effect=fake):
            with self.assertRaises(RuntimeError) as cm:
                site().fetch(None, "ua", 5)
        msg = str(cm.exception)
        self.assertTrue(re.search(r"Store API: .*no product", msg), msg)
        self.assertIn("variations JSON not found", msg)


if __name__ == "__main__":
    unittest.main()
