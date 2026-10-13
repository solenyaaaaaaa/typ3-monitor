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
        Session=lambda: object(), get=lambda *args, **kwargs: None,
        HTTPError=type("HTTPError", (Exception,), {}),
    )
import monitor  # noqa: E402


CFG = {
    "max_concentrate_grams": 5,
    "exotic_keywords": ["exotic"],
    "realert_after_hours": 12,
}

# A cut-down copy of the real page's structure (2026-10-13): banner images
# label the lists, two <li> shapes, an update card, production notes, and a
# footer whose text must be ignored.
PAGE = """
<html><body><header>nav</header>
<div data-elementor-type="wp-page" data-elementor-id="9189">
 <h2 class="elementor-heading-title elementor-size-default">VERITAS Production Updates - 10/11</h2>
 <div class="elementor-element elementor-widget elementor-widget-text-editor" data-widget_type="text-editor.default">
  <div class="elementor-widget-container"><ol>
   <li><strong>Infused Chocolate Pre-Order</strong>: 50mg and 100mg Hash and or rosin infused Bon Bons.</li>
   <li><strong>Exotic Zips</strong>: Cherry Runtz is back in zips.</li></ol>
  </div></div>
 <div class="elementor-element elementor-widget elementor-widget-shortcode" data-widget_type="shortcode.default">
  <article
	class="vb-card"
	data-vb-cats="live-rosin"
	data-vb-text="x">
   <h3 class="vb-card__title">Tier 1 Pheno Hunt Rosin - Madd Peach</h3>
   <p class="vb-card__note">30g Available</p>
   <p class="vb-card__price"><span>$</span>26.00</p>
   <a class="vb-btn" href="https://veritasthca.com/product/tier-1-pheno-hunt-rosin-madd-peach/" rel="noopener"> View Product </a>
  </article>
 </div>
 <div class="elementor-element elementor-widget elementor-widget-image" data-widget_type="image.default">
  <img src="https://i0.wp.com/veritasthca.com/wp-content/uploads/2026/09/Small-Lots-Flyer-2-1.jpeg?fit=1024" alt="" /></div>
 <div class="elementor-element elementor-widget elementor-widget-price-list" data-widget_type="price-list.default">
  <ul class="elementor-price-list">
   <li><a class="elementor-price-list-item" href="https://veritasthca.com/product/tier-2-socal-lab-sour-strawberry/">
    <span class="elementor-price-list-title">Sour Strawberry - Tier 2</span>
    <span class="elementor-price-list-price">$18.00 per gram | 10g MOQ</span>
    <p class="elementor-price-list-description">40g Lot - 10g MOQ</p></a></li>
   <li class="elementor-price-list-item">
    <span class="elementor-price-list-title">WTR x Vapors - Tier 1 Pheno Hunt</span>
    <span class="elementor-price-list-price">$26.00 per gram | 10g MOQ</span>
    <p class="elementor-price-list-description">20g available - 10g MOQ</p></li>
  </ul></div>
 <div class="elementor-element elementor-widget elementor-widget-image" data-widget_type="image.default">
  <img src="https://i0.wp.com/veritasthca.com/wp-content/uploads/2026/06/VERITAS-FLOWER-MENU.png" alt="Best Wholesale Thca Flower" /></div>
 <div class="elementor-element elementor-widget elementor-widget-price-list" data-widget_type="price-list.default">
  <ul class="elementor-price-list">
   <li><a class="elementor-price-list-item" href="#">
    <span class="elementor-price-list-title">Zkittles ( AAA Indoor Smalls )</span>
    <span class="elementor-price-list-price">$750.00</span></a></li>
  </ul></div>
</div>
<div data-elementor-type="footer" data-elementor-id="3184">
 <div class="elementor-element elementor-widget elementor-widget-text-editor" data-widget_type="text-editor.default">
  <p>Copyright 2026. Exotic zips, 1g rosin jars, everything.</p></div>
</div></body></html>
"""


def product(pid, name, cats=("rosin",), cart_min=1, options=(), variations=(),
            short="", desc="", tags=(), in_stock=True, slug=None, price="$20.00",
            variation_stock=None):
    return {
        "id": pid, "slug": slug or "p%d" % pid, "name": name,
        "url": "https://veritasthca.com/product/%s/" % (slug or "p%d" % pid),
        "cats": list(cats), "tags": list(tags), "in_stock": in_stock,
        "cart_min": cart_min, "options": list(options),
        "variations": [{"id": vid, "label": label} for vid, label in variations],
        "short": short, "desc": desc, "price": price,
        "variation_stock": dict(variation_stock or {}),
    }


def menu_line(title, price="", desc="", href=None, section=""):
    return {"source": "menu", "title": title, "texts": [price, desc], "href": href,
            "section": section, "cats": []}


def note(text):
    return {"source": "note", "title": text, "texts": [], "href": None, "section": "", "cats": []}


def snapshot(products=(), menu=(), cards=(), notes=()):
    return {"products": list(products), "menu": list(menu), "cards": list(cards),
            "notes": list(notes)}


# The day-one catalog in miniature: everything here must stay silent.
BASELINE = [
    product(11749, "Tier 1 Exotic Rosin – Blue Razz", cats=("extracts", "rosin")),
    product(9711, "Tier 3 Rosin – Banana Punch", cart_min=28),
    product(5712, "Las Vegas Tier 1 – Banana Punch",
            options=("Live Rosin - Weight: 100g", "Live Rosin - Weight: 10g",
                     "Live Rosin - Weight: 28g", "Live Rosin - Weight: 56g")),
    product(11981, "A Grade Indoor Smalls Flower – Black Ice ( OZ)", cats=("flower",),
            short="( Oz PRICING ) Black Ice is an indica dominant hybrid", price="$60.00"),
    product(4196, "All NBA – Sugar", cats=("thca-sugar-resin",),
            variations=((1, "28g"), (2, "56g"), (3, "112g"))),
    product(10900, "Hash Infused Prerolls – 1 Gram Retail Ready", cats=("prerolls",),
            cart_min=10),
    product(4744, "Ikrusher Uzo 1g Disposable – OG Light Live Resin 80% Rosin",
            cats=("clearance-closeouts", "vape")),
]


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.site = monitor.VeritasSite(CFG)

    def test_menu_cards_and_notes_come_from_the_page_document_only(self):
        menu, cards, notes = self.site._parse_menu(PAGE)
        self.assertEqual([m["title"] for m in menu], [
            "Sour Strawberry - Tier 2", "WTR x Vapors - Tier 1 Pheno Hunt",
            "Zkittles ( AAA Indoor Smalls )"])
        self.assertEqual(menu[0]["texts"], ["$18.00 per gram | 10g MOQ", "40g Lot - 10g MOQ"])
        self.assertEqual(menu[0]["href"],
                         "https://veritasthca.com/product/tier-2-socal-lab-sour-strawberry/")
        self.assertIsNone(menu[1]["href"])
        # Banner image names label the lists.
        self.assertIn("small lots flyer", menu[0]["section"])
        self.assertIn("flower", menu[2]["section"])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["cats"], ["live-rosin"])
        self.assertEqual(cards[0]["texts"][0], "30g Available")
        texts = [n["title"] for n in notes]
        self.assertTrue(any("Cherry Runtz is back in zips" in t for t in texts))
        self.assertFalse(any("Copyright" in t for t in texts), "footer text leaked in")

    def test_size_context_separates_lot_size_from_moq(self):
        ctx = [(g, c) for g, c, _ in self.site._size_mentions("20g available - 10g MOQ")]
        self.assertEqual(ctx, [(20.0, "lot"), (10.0, "moq")])
        self.assertEqual([c for _, c, _ in self.site._size_mentions("40g Lot - 10g MOQ")],
                         ["lot", "moq"])
        self.assertEqual([c for _, c, _ in self.site._size_mentions(
            "Limited amount of stock available - only 10g")], ["lot"])
        self.assertEqual([(g, c) for g, c, _ in self.site._size_mentions("MOQ 28 grams")],
                         [(28.0, "moq")])
        self.assertEqual([c for _, c, _ in self.site._size_mentions("2 Gram Jar")], ["unit"])
        # "1/2 g" is half a gram, never 2 g.
        self.assertEqual([g for g, _, _ in self.site._size_mentions("1/2 g samples")], [0.5])

    def test_ounce_means_exactly_one_ounce(self):
        yes = ["AAA Indoor Exotic – Cherry Runtz Ounces", "Black Ice ( OZ)", "Exotic Zips",
               "28g", "$80.00 per oz", "1 oz"]
        no = ["QP (4 oz)", "1/2 oz", "half zip", "GMO x OZK", "128g", "8 oz bag", "Half Pound"]
        for text in yes:
            self.assertTrue(self.site._ounce_mentions(text), text)
        for text in no:
            self.assertFalse(self.site._ounce_mentions(text), text)


class MinPurchaseTests(unittest.TestCase):
    def setUp(self):
        self.site = monitor.VeritasSite(CFG)

    def grams(self, **kw):
        return self.site._min_purchase(**kw)[0]

    def test_default_cart_minimum_is_no_evidence(self):
        # The menu says "10g MOQ" on these; a cart minimum of 1 is the default.
        self.assertIsNone(self.grams(ident="Tier 1 Exotic Rosin – Blue Razz", cart_min=1))

    def test_explicit_cart_minimums_count(self):
        self.assertEqual(self.grams(ident="Tier 3 Rosin", cart_min=28), 28)
        self.assertEqual(self.grams(ident="Tier 1 Rosin", cart_min=5), 5)

    def test_stated_sizes_and_moqs(self):
        self.assertEqual(self.grams(ident="Zina the MVP Tier 1 Exotic | Pheno Hunt 2g"), 2)
        self.assertEqual(self.grams(texts=["$27.00 per gram | 10g MOQ"]), 10)
        self.assertEqual(self.grams(texts=["$27.00 per gram | 1g MOQ"]), 1)
        self.assertEqual(self.grams(texts=["No MOQ"]), 1)
        self.assertEqual(self.grams(texts=["No MOQ"], cart_min=10), 10)
        self.assertEqual(self.grams(texts=["Sold in 1g jars - MOQ 10"]), 10)
        self.assertEqual(self.grams(options=["Live Rosin - Weight: 10g",
                                             "Live Rosin - Weight: 28g"]), 10)

    def test_prose_sizes_do_not_count_in_descriptions(self):
        self.assertIsNone(self.grams(texts=["We pressed 5g of this cut for testing."]))
        self.assertIsNone(self.grams(texts=["30g Available"]))


class VeritasDiffTests(unittest.TestCase):
    def setUp(self):
        self.site = monitor.VeritasSite(CFG)
        self.seed, alerts = self.site.diff({}, snapshot(BASELINE))
        self.assertEqual(self.seed["available"], {})

    def alerts_for(self, snap, prev=None):
        return self.site.diff(self.seed if prev is None else prev, snap)[1]

    def test_day_one_catalog_is_silent(self):
        self.assertEqual(self.alerts_for(snapshot(BASELINE, menu=[
            menu_line("Blue Razz - Tier 1 Exotic", "$27.00 per gram | 10g MOQ",
                      "True Exotic - micro batch production. 10g MOQ"),
            menu_line("Lantz x GG4 - Tier 3", "$14.00 per gram | 10g MOQ", "only 5g left"),
        ], notes=[note("Infused Chocolate Pre-Order: 50mg and 100mg Hash and or rosin "
                       "infused Bon Bons, 2g each."),
                  note("We pressed 5g of Zina this morning, more soon.")])), [])

    def test_small_rosin_listing_alerts_once(self):
        zina = product(9769, "Zina the MVP Tier 1 Exotic |  Pheno Hunt 2g",
                       desc="Micro Batch – Exotic Cross 2 Gram Jar", price="$80.00")
        state, alerts = self.site.diff(self.seed, snapshot(BASELINE + [zina]))
        self.assertEqual([(a["kind"], a["title"]) for a in alerts],
                         [("ROSIN_5G", "Zina the MVP Tier 1 Exotic |  Pheno Hunt 2g")])
        self.assertIn("2 g", alerts[0]["details"])
        self.assertEqual(self.site.diff(state, snapshot(BASELINE + [zina]))[1], [])

    def test_exotic_ounce_alerts_and_plain_aaa_ounce_does_not(self):
        exotic = product(10693, "AAA Indoor Exotic – Cherry Runtz Ounces", cats=("flower",),
                         variations=((10694, "28g"),), price="$80.00")
        plain = product(11091, "AAA Indoor – Purple Pixie Dust Ounces", cats=("flower",),
                        variations=((11092, "28g"),), price="$70.00")
        eighths = product(11339, "AAA Exotic Flower – Nuclear Slurpee | 3.5 g Jars",
                          cats=("clearance-closeouts", "flower"))
        alerts = self.alerts_for(snapshot(BASELINE + [exotic, plain, eighths]))
        self.assertEqual([(a["kind"], a["title"]) for a in alerts],
                         [("EXOTIC_OZ", "AAA Indoor Exotic – Cherry Runtz Ounces")])

    def test_ounce_variation_must_itself_be_in_stock(self):
        mixed = dict(pid=990, name="AAA Indoor Exotic – Test Strain", cats=("flower",),
                     variations=((991, "28g"), (992, "112g"), (993, "454g")))
        sold_out = product(variation_stock={991: False}, **mixed)
        self.assertEqual(self.alerts_for(snapshot(BASELINE + [sold_out])), [])
        in_stock = product(variation_stock={991: True}, **mixed)
        self.assertEqual([a["kind"] for a in self.alerts_for(snapshot(BASELINE + [in_stock]))],
                         ["EXOTIC_OZ"])

    def test_vapes_edibles_and_resin_never_count(self):
        noise = [
            product(1, "Half Gram Live Rosin Disposable – Tallymon (Single)", cats=("vape",)),
            product(2, "Tier 1 Exotic Rosin Disposable – Paradise Pine", cats=("rosin",)),
            product(3, "100mg Hash Rosin Bon Bons", cats=("edibles",)),
            product(4, "Exotic Live Resin – Gluetopia", cats=(),
                    variations=((41, "28g"), (42, "56g"))),
        ]
        self.assertEqual(self.alerts_for(snapshot(BASELINE + noise)), [])

    def test_menu_line_counts_only_while_its_product_is_buyable(self):
        line = menu_line("Sour Strawberry - Tier 2", "$18.00 per gram | 2g MOQ",
                         href="https://veritasthca.com/product/sold-out-strawberry/")
        self.assertEqual(self.alerts_for(snapshot(BASELINE, menu=[line])), [])
        listed = product(3698, "Tier 2 SOCAL Lab – Sour Strawberry", slug="sold-out-strawberry")
        alerts = self.alerts_for(snapshot(BASELINE + [listed], menu=[line]))
        self.assertEqual([a["kind"] for a in alerts], ["ROSIN_5G"])
        self.assertEqual(alerts[0]["url"], "https://veritasthca.com/product/sold-out-strawberry/")

    def test_unlinked_menu_lines_and_notes(self):
        alerts = self.alerts_for(snapshot(BASELINE, menu=[
            menu_line("Blue Orangeade - Tier 1 Exotic", "$27.00 per gram | 1g MOQ"),
            menu_line("Cherry Runtz - AAA Exotic", "$80.00 per oz", section="veritas flower menu"),
        ], notes=[note("Tier 1 rosin is now available in 2g jars while supplies last.")]))
        self.assertEqual(sorted(a["kind"] for a in alerts),
                         ["EXOTIC_OZ", "ROSIN_5G", "ROSIN_5G"])

    def test_uncategorised_rosin_is_recognised_by_its_tier(self):
        limez = product(9592, "LV Lab Tier 1 – Lemon Limez | 2g Jars", cats=())
        self.assertEqual([a["kind"] for a in self.alerts_for(snapshot(BASELINE + [limez]))],
                         ["ROSIN_5G"])

    def test_exotic_from_menu_title_plus_ounce_from_product(self):
        aaa = product(70, "AAA Indoor – Cherry Runtz Ounces", cats=("flower",),
                      slug="cherry-runtz-ounces", variations=((71, "28g"),))
        line = menu_line("Cherry Runtz - AAA Exotic", "$80.00",
                         href="https://veritasthca.com/product/cherry-runtz-ounces/")
        self.assertEqual([a["kind"] for a in self.alerts_for(snapshot(BASELINE + [aaa], menu=[line]))],
                         ["EXOTIC_OZ"])

    def test_restock_inside_cooldown_is_one_event(self):
        zina = product(9769, "Zina the MVP Tier 1 Exotic | Pheno Hunt 2g")
        on, alerts = self.site.diff(self.seed, snapshot(BASELINE + [zina]))
        self.assertEqual(len(alerts), 1)
        off, _ = self.site.diff(on, snapshot(BASELINE))
        self.assertEqual(self.site.diff(off, snapshot(BASELINE + [zina]))[1], [])
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
        off["alerted_at"] = {k: long_ago for k in off["alerted_at"]}
        again = self.site.diff(off, snapshot(BASELINE + [zina]))[1]
        self.assertEqual(len(again), 1)
        self.assertTrue(again[0]["details"].endswith("back in stock"))


class FakeResponse:
    def __init__(self, text="", data=None, headers=None):
        self.text = text
        self._data = data
        self.headers = headers or {}

    def json(self):
        return self._data


class FetchFailureTests(unittest.TestCase):
    def setUp(self):
        self.site = monitor.VeritasSite(dict(CFG, exclude_category_ids=[786295542]))

    def test_menu_without_price_list_raises(self):
        with patch.object(monitor, "http_get", return_value=FakeResponse("<html>redesign</html>")), \
             patch.object(monitor.time, "sleep"):
            with self.assertRaises(RuntimeError):
                self.site.fetch(None, "ua", 1)

    def test_empty_catalog_raises(self):
        responses = [FakeResponse(PAGE), FakeResponse(data=[], headers={"X-WP-TotalPages": "1"})]
        with patch.object(monitor, "http_get", side_effect=responses) as get, \
             patch.object(monitor.time, "sleep"):
            with self.assertRaises(RuntimeError):
                self.site.fetch(None, "ua", 1)
        api_url = get.call_args_list[1][0][1]
        self.assertIn("stock_status=instock,onbackorder", api_url)
        self.assertIn("category=786295542&category_operator=not_in", api_url)


if __name__ == "__main__":
    unittest.main()
