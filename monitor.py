"""Multi-site cannabis storefront drop monitor.

Polls a configured list of storefronts, diffs against a saved state file,
and fires alerts (email always; Windows toasts + sound + browser when run
on Windows) when new products / strains / variants appear or come back in
stock.

Designed to run on a 5-min schedule via GitHub Actions (Linux) or local
Windows Task Scheduler.
"""

import argparse
import html as htmllib
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import winsound
    try:
        from winotify import Notification, audio
        HAS_TOAST = True
    except ImportError:
        HAS_TOAST = False
else:
    winsound = None
    HAS_TOAST = False


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "state.json"
LOG_PATH = SCRIPT_DIR / "monitor.log"
SECRETS_PATH = (
    Path(os.environ.get("APPDATA", str(Path.home())))
    / "typ3-monitor" / "secrets.json"
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "user_agent": DEFAULT_USER_AGENT,
    "timeout_sec": 25,
    "email_enabled": True,
    # email_to is intentionally NOT in the repo. It is sourced from the
    # EMAIL_TO env var (cloud) or the local secrets.json file.
    "email_subject_prefix": "[Drop Monitor]",
    "show_toast": True,
    "play_sound": True,
    "open_browser_on_alert": True,
    "max_browser_tabs_per_run": 5,
    # A single failed request is ordinary internet noise. Consecutive real
    # failures are stored separately from catalog state so they can make the
    # monitor unhealthy without stopping the other sites from being checked.
    "site_failure_threshold": 3,
    "typ3": {
        "enabled": True,
        "collection_urls": [
            "https://typ3cannabis.com/collections/flower",
            "https://typ3cannabis.com/collections/extracts",
            "https://typ3cannabis.com/collections/vapes",
            "https://typ3cannabis.com/collections/edibles",
        ],
        "ignore_handles": [
            "test-payment",
            "typ3-unisex-t-shirt",
            "typ3-pullover-hoodie",
            "typ3-canvas-tote-bag",
            "typ3-sticker-sheet",
            "typ3-dad-hat",
            "typ3-hoodie",
            "typ3-logo-tee",
            "stainless-steel-water-bottle-with-a-straw-lid",
            "mug-with-color-inside",
            "vintage-corduroy-cap",
            "trucker-cap",
            "embroidered-patches",
            "snapback-hat",
            "muscle-shirt",
            "pom-pom-beanie",
        ],
    },
    "hempbarn_livingsoil": {
        "enabled": True,
        "url": "https://www.thehempbarn.com/product/livingsoil/",
        "description_keywords": ["special", "all time", "all-time", "10/10"],
    },
    "hempbarn_organicsoil": {
        "enabled": True,
        "url": "https://www.thehempbarn.com/product/organicsoil/",
        "description_keywords": ["special", "all time", "all-time", "10/10"],
    },
    "caregiverpharms": {
        "enabled": True,
        "products_json_url": "https://caregiverpharms.com/products.json?limit=250",
        "collection_url": "https://caregiverpharms.com/collections/all",
        "smalls_keywords": ["smalls", "micros"],
    },
    "flowgardens_smalls": {
        "enabled": True,
        "product_json_url": "https://flowgardens.com/products/smalls.json",
        "product_url": "https://flowgardens.com/products/smalls",
        "allowed_types": [2],
    },
    "fiveleafwellness": {
        "enabled": True,
        "products_api_url": "https://fiveleafwellness.com/wp-json/wc/store/v1/products",
        "shop_url": "https://fiveleafwellness.com/shop/",
        "target_categories": ["top-shelf"],
    },
    "beleafer_indoor": {
        "enabled": True,
        "category_url": "https://beleafer.com/product-category/hemp-flower/indoor/",
        "allowed_types": [2],
        "max_pages": 20,
        "min_interval_minutes": 5,
        "impersonate": True,
    },
    "highalpinegenetics": {
        "enabled": True,
        "search_url": "https://www.highalpinegenetics.com/apps/search?q=+",
        "allowed_types": [1, 2],
        "max_pages": 30,
        "min_interval_minutes": 5,
    },
    "antheiafarms": {
        "enabled": True,
        "products_json_url": "https://antheiafarms.myshopify.com/collections/all/products.json?limit=250",
        "collection_url": "https://antheiafarms.myshopify.com/collections/all",
    },
    "veritas": {
        "enabled": True,
        "menu_url": "https://veritasthca.com/veritas-drop-menu/",
        "products_api_url": "https://veritasthca.com/wp-json/wc/store/v1/products",
        "max_concentrate_grams": 5,
        "exotic_keywords": ["exotic"],
        # thca-sugar-resin, vape, edibles, prerolls
        "exclude_category_ids": [786295542, 786295536, 786295535, 786295543],
        "realert_after_hours": 12,
        "min_interval_minutes": 3,
    },
}


# ---------- I/O helpers --------------------------------------------------

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _email_to_from_secrets_file():
    if not SECRETS_PATH.exists():
        return None
    try:
        s = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
        return s.get("email_to")
    except Exception:
        return None


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8"
        )
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
    else:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        merged = _deep_merge(DEFAULT_CONFIG, cfg)
    # email_to is never stored in the repo. Pull it from env (cloud)
    # or from the local secrets file (Windows). If neither, email is disabled.
    if not merged.get("email_to"):
        merged["email_to"] = (
            os.environ.get("EMAIL_TO")
            or _email_to_from_secrets_file()
            or ""
        )
    return merged


def load_state():
    if not STATE_PATH.exists():
        return {"sites": {}, "last_run": None}
    s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if "sites" not in s:
        legacy_typ3 = {}
        if "products" in s:
            legacy_typ3["products"] = s["products"]
        s = {"sites": {"typ3": legacy_typ3}, "last_run": s.get("last_run")}
    return s


def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def record_site_failure(state, site_name, exc, threshold):
    """Record one attempted fetch/diff failure and return its capped count."""
    failures = state.setdefault("health", {}).setdefault("site_failures", {})
    previous = failures.get(site_name, {})
    count = min(int(previous.get("count", 0)) + 1, threshold)
    now_iso = datetime.now(timezone.utc).isoformat()
    failures[site_name] = {
        "count": count,
        # Preserved across the streak: `count` is capped at the threshold, so
        # it cannot distinguish "failing for 3 minutes" from "failing for 3
        # days". Duration is what decides whether a human should care.
        "first_failed_at": previous.get("first_failed_at") or now_iso,
        "last_error": str(exc)[:300],
        "last_failed_at": now_iso,
    }
    return count


def clear_site_failure(state, site_name):
    """Clear a site's failure record after a successful fetch and diff."""
    state.setdefault("health", {}).setdefault("site_failures", {}).pop(site_name, None)


def persistent_site_failures(state, threshold, active_site_names):
    failures = state.setdefault("health", {}).setdefault("site_failures", {})
    for site_name in set(failures) - set(active_site_names):
        failures.pop(site_name)
    return {
        name: entry for name, entry in failures.items()
        if int(entry.get("count", 0)) >= threshold
    }


def degraded_site_failures(persistent_failures, degraded_after_minutes):
    """Of the sites currently failing, which have been failing long enough to
    be worth a human's attention?

    Beleafer's Cloudflare block comes and goes within minutes. Treating three
    consecutive failures as an incident meant a transient block and a genuinely
    dead site looked identical. Duration separates them.
    """
    if degraded_after_minutes <= 0:
        return dict(persistent_failures)
    now = datetime.now(timezone.utc)
    out = {}
    for name, entry in persistent_failures.items():
        started = entry.get("first_failed_at")
        if not started:
            continue
        try:
            began = datetime.fromisoformat(started)
        except ValueError:
            continue
        if (now - began).total_seconds() / 60.0 >= degraded_after_minutes:
            out[name] = entry
    return out


def load_secrets():
    env_pw = os.environ.get("SMTP_PASSWORD")
    if env_pw:
        return {
            "smtp_host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
            "smtp_user": os.environ.get("SMTP_USER", ""),
            "smtp_password": env_pw,
        }
    if not SECRETS_PATH.exists():
        logging.warning("no secrets file at %s; email disabled this run", SECRETS_PATH)
        return None
    try:
        secrets = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.error("failed to parse secrets file: %s", exc)
        return None
    required = ("smtp_host", "smtp_port", "smtp_user", "smtp_password")
    missing = [k for k in required if not secrets.get(k)]
    if missing:
        logging.error("secrets file is missing keys: %s", missing)
        return None
    return secrets


# ---------- HTTP --------------------------------------------------------

# Browser fingerprints to present when a WAF refuses ours (see http_get), in
# the order tried. curl_cffi's bare aliases mean "its newest target of that
# browser", and the workflow installs the newest curl_cffi on every run, so
# these stay current with no edits. NEVER pin a version. On 2026-10-13
# Beleafer's host answered 403 to every Chrome fingerprint up to 136 --
# including the pinned "chrome124" this used to be -- and 200 to Chrome 142+,
# Safari and Firefox from the same IP. Real Chrome auto-updates, so an old
# Chrome fingerprint reads as a bot, and a pinned one only gets older:
# Beleafer was dark for 29 days (2026-09-14 to 2026-10-13) on exactly that.
# Optional on purpose: if the wheel is unavailable the monitor still runs, it
# just loses the ability to get past a fingerprint block.
BROWSER_IMPERSONATE = ("chrome", "safari", "firefox")
try:
    from curl_cffi import requests as _curl_cffi_requests
    _impersonating_get = _curl_cffi_requests.get
except Exception:  # pragma: no cover - depends on the install environment
    _impersonating_get = None
    logging.info("curl_cffi unavailable; WAF-blocked sites cannot be retried")

# host -> the fingerprint that last got through, so the rest of a run's
# requests to that host (Beleafer: a listing, then product pages) lead with it.
_IMPERSONATION_THAT_WORKED = {}


def _impersonated_get(url, accept, timeout, expect_pattern=None):
    """GET `url` with real browsers' TLS/HTTP2 fingerprints, trying each in
    BROWSER_IMPERSONATE until one is let through. A 403/429, or a 200 missing
    `expect_pattern`, moves on to the next fingerprint; any other status (a
    404 that ends pagination, say) is final. Returns the first good response,
    else the last one received, else re-raises the last error."""
    host = url.split("/")[2] if "://" in url else url
    order = list(BROWSER_IMPERSONATE)
    worked = _IMPERSONATION_THAT_WORKED.get(host)
    if worked in order:
        order.remove(worked)
        order.insert(0, worked)
    last_resp = last_exc = None
    for target in order:
        try:
            # Send ONLY Accept and let curl_cffi supply the rest. Passing our
            # own User-Agent overrides the impersonated one, and the WAF checks
            # that the UA agrees with the TLS fingerprint and header order, so
            # a mismatched pair is rejected exactly like plain requests was.
            # Verified against Beleafer 2026-09-09: with our UA it stayed 403,
            # without it 200.
            resp = _impersonating_get(url, headers={"Accept": accept},
                                      timeout=timeout, impersonate=target)
        except Exception as exc:
            last_exc = exc
            continue
        resp.encoding = "utf-8"
        blocked = resp.status_code in (403, 429) or (
            resp.status_code < 400 and expect_pattern is not None
            and not expect_pattern.search(resp.text))
        if not blocked:
            if resp.status_code < 400:
                _IMPERSONATION_THAT_WORKED[host] = target
            return resp
        last_resp = resp
    if last_resp is not None:
        return last_resp
    raise last_exc


def http_get(session, url, ua, timeout, expect_json=False, expect_pattern=None,
             impersonate=False):
    """GET `url`, falling back to real browser fingerprints when a WAF
    refuses plain `requests`. `impersonate=True` skips the plain attempt, for
    a site that refuses it every time: each poll then stops opening with a
    request the WAF already scores as a bot."""
    accept = ("application/json" if expect_json else
              "text/html,application/xhtml+xml,application/xml;q=0.9")
    headers = {
        "User-Agent": ua,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if impersonate and _impersonating_get is not None:
        resp = _impersonated_get(url, accept, timeout, expect_pattern)
        if resp.status_code >= 400:
            # requests.HTTPError, not curl_cffi's: see the note below.
            raise requests.HTTPError(
                "%s Error for url: %s" % (resp.status_code, url), response=resp)
        return resp
    try:
        resp = session.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status not in (403, 429) or _impersonating_get is None:
            raise
        # A WAF fingerprints the TLS/HTTP2 handshake, not the User-Agent
        # string, so `requests` is rejected no matter what headers it sends.
        # Beleafer switched this on around 2026-09-09 06:00 UTC and started
        # 403ing EVERY request including robots.txt, which silently took that
        # site offline. Retrying with a real browser fingerprint restores 200.
        # Kept generic rather than Beleafer-specific: any monitored shop can
        # turn this on overnight, and the fallback costs nothing until a
        # 403/429 actually happens.
        logging.info("HTTP %s from %s; retrying with a browser fingerprint",
                     status, url)
        resp = _impersonated_get(url, accept, timeout)
        if resp.status_code >= 400:
            # Raise requests.HTTPError, NOT curl_cffi's, so every existing
            # `except requests.HTTPError` handler keeps working. Beleafer's
            # pagination loop relies on catching a 404 to detect the last page;
            # a curl_cffi exception escapes it and kills the whole fetch.
            raise requests.HTTPError(
                "%s Error for url: %s" % (resp.status_code, url), response=resp)
    resp.encoding = "utf-8"
    if (expect_pattern is not None and _impersonating_get is not None
            and not expect_pattern.search(resp.text)):
        # HTTP 200 with the expected payload missing. A WAF can return a
        # challenge or a stripped page under a 200, so no status-code check
        # catches this -- it surfaces later as a confusing parse error. Seen
        # 2026-09-09: thehempbarn.com served the full page to a residential IP
        # and a contentless 200 to the GitHub runner, which read as "variations
        # JSON not found on page". Retry with browser fingerprints.
        logging.info("expected content missing from %s; retrying impersonated", url)
        try:
            alt = _impersonated_get(url, accept, timeout, expect_pattern)
            if alt.status_code < 400 and expect_pattern.search(alt.text):
                return alt
        except Exception as exc:
            logging.info("impersonated retry of %s failed: %s", url, exc)
    return resp


# ---------- Shared re-check diff (late-signal-safe) ---------------------

DEFAULT_RECHECK_WINDOW_HOURS = 8
DEFAULT_MAX_RECHECKS_PER_RUN = 25


def recheck_listing_diff(prev_state, curr_urls, now, *, evaluate,
                         window_hours=DEFAULT_RECHECK_WINDOW_HOURS,
                         max_per_run=DEFAULT_MAX_RECHECKS_PER_RUN):
    """Diff for sites whose match signal lives on a separately-fetched detail
    page and can appear AFTER the product is first listed.

    The naive "check each product once when it first appears, then never
    again" pattern silently drops a product whose qualifying signal (a type
    designation, a tier, a keyword) is written to its detail page minutes or
    hours after the product is listed. This helper instead re-checks a
    not-yet-matched product on every run until it matches (alert once) or its
    re-check window lapses -- after which an unmatched product is treated as
    genuinely non-qualifying and is no longer refetched, bounding load to
    recently-appeared products.

    Args:
        prev_state:  previously saved state dict for the site.
        curr_urls:   iterable of product URLs currently in the listing.
        now:         timezone-aware datetime for this run.
        evaluate(url) -> (matched: bool, record: dict, alert: dict | None):
            fetches/parses the detail page and decides whether it qualifies.
            ``record`` is per-URL data to persist; ``alert`` is emitted when
            matched.
        window_hours: how long after first sighting to keep re-checking.
        max_per_run:  cap on detail-page evaluations per run (load guard).

    Returns ``(new_state, alerts)``. Per-URL state carries ``first_seen``,
    ``alerted`` and ``checks``. Migration / first run (state lacks the schema
    marker ``v == 2``) seeds the current listing as already-handled (start
    clean) and emits no alerts.
    """
    prev = prev_state.get("products", {})
    now_iso = now.isoformat()

    if prev_state.get("v") != 2:
        # Pre-upgrade or genuine first run: start clean. Everything currently
        # known is marked handled so the switch never retro-alerts; only
        # products that appear on a LATER run can fire.
        seeded = {
            url: {"first_seen": now_iso, "alerted": True, "checks": 0}
            for url in (set(prev.keys()) | set(curr_urls))
        }
        return {"v": 2, "products": seeded, "last_seen": now_iso}, []

    window = timedelta(hours=window_hours)
    products = {}
    alerts = []

    # Carry forward products no longer listed so they never re-alert if they
    # reappear later.
    for url, rec in prev.items():
        if url not in curr_urls:
            products[url] = rec

    # Decide which listed products still need (re)checking this run.
    candidates = []
    for url in curr_urls:
        rec = dict(prev.get(url, {}))
        rec.setdefault("first_seen", now_iso)
        rec.setdefault("alerted", False)
        rec.setdefault("checks", 0)
        products[url] = rec
        if rec["alerted"]:
            continue
        try:
            within = (now - datetime.fromisoformat(rec["first_seen"])) <= window
        except Exception:
            within = True
        if within:
            candidates.append(url)
        # else: window lapsed -- keep the record, stop refetching quietly.

    # Prioritise least-checked (new products first), then oldest, and cap the
    # detail fetches per run. Unprocessed candidates stay unalerted and within
    # window, so they are picked up on a subsequent run.
    candidates.sort(key=lambda u: (products[u]["checks"], products[u]["first_seen"]))
    for url in candidates[:max_per_run]:
        rec = products[url]
        matched, record, alert = evaluate(url)
        if record:
            rec.update(record)
        rec["checks"] = rec.get("checks", 0) + 1
        if matched:
            rec["alerted"] = True
            if alert:
                alerts.append(alert)

    return {"v": 2, "products": products, "last_seen": now_iso}, alerts


# ---------- Site adapters -----------------------------------------------

class Typ3Site:
    name = "typ3"
    label = "TYP3 Cannabis"
    PRODUCT_BASE = "https://typ3cannabis.com/products/"

    CARD_RE = re.compile(
        r'<a class="group h-full" href="/products/(?P<handle>[a-z0-9-]+)">'
        r'(?P<body>.*?)</a></li>',
        re.DOTALL,
    )
    TITLE_RE = re.compile(
        r'data-testid="product-title"[^>]*>(?P<title>[^<]+)</p>'
    )
    PRICE_RE = re.compile(
        r'data-testid="price"[^>]*>(?P<price>.*?)</p>',
        re.DOTALL,
    )
    SOLD_OUT_MARKER = "sold-out-card"
    MADE_TO_ORDER_MARKER = "Made to Order"

    def __init__(self, cfg):
        self.cfg = cfg
        self.ignore = set(cfg.get("ignore_handles", []))

    @classmethod
    def _clean(cls, s):
        s = re.sub(r"<[^>]+>", "", s)
        return re.sub(r"\s+", " ", s).strip()

    @classmethod
    def _parse_page(cls, html):
        out = []
        for m in cls.CARD_RE.finditer(html):
            handle = m.group("handle")
            body = m.group("body")
            t = cls.TITLE_RE.search(body)
            p = cls.PRICE_RE.search(body)
            out.append({
                "handle": handle,
                "title": cls._clean(t.group("title")) if t else handle,
                "price": cls._clean(p.group("price")) if p else "",
                "sold_out": cls.SOLD_OUT_MARKER in body,
                "made_to_order": cls.MADE_TO_ORDER_MARKER in body,
            })
        return out

    def fetch(self, session, ua, timeout):
        catalog = {}
        # Site moved from /store?page=N to per-category /collections/<name>.
        urls = self.cfg.get("collection_urls") or []
        for url in urls:
            try:
                resp = http_get(session, url, ua, timeout)
            except Exception as exc:
                logging.warning("[%s] %s fetch failed: %s", self.name, url, exc)
                continue
            products = self._parse_page(resp.text)
            for p in products:
                # If a handle appears in multiple collections, prefer the
                # available copy (more accurate stock state).
                existing = catalog.get(p["handle"])
                if existing and not existing["sold_out"] and p["sold_out"]:
                    continue
                catalog[p["handle"]] = p
            time.sleep(0.4)
        return catalog

    def diff(self, prev_state, current):
        now_iso = datetime.now(timezone.utc).isoformat()
        prev = prev_state.get("products", {})
        alerts = []
        for handle, prod in current.items():
            if handle in self.ignore:
                continue
            url = self.PRODUCT_BASE + handle
            if handle not in prev:
                if not prod["sold_out"] and not prod["made_to_order"]:
                    alerts.append({
                        "site": self.name, "label": self.label,
                        "kind": "DROP", "title": prod["title"],
                        "url": url, "details": prod["price"],
                    })
            else:
                was_avail = (
                    not prev[handle].get("sold_out", False)
                    and not prev[handle].get("made_to_order", False)
                )
                now_avail = (
                    not prod["sold_out"] and not prod["made_to_order"]
                )
                if now_avail and not was_avail:
                    alerts.append({
                        "site": self.name, "label": self.label,
                        "kind": "RESTOCK", "title": prod["title"],
                        "url": url, "details": prod["price"],
                    })
        # Preserve previously-seen handles so that a product flickering off
        # a collection page does not get re-detected as a DROP next time.
        new_state = {"products": dict(prev)}
        for handle, prod in current.items():
            prior = prev.get(handle, {})
            new_state["products"][handle] = {
                **prod,
                "first_seen": prior.get("first_seen", now_iso),
                "last_seen": now_iso,
            }
        return new_state, alerts


class HempBarnSite:
    """Base for Hemp Barn product pages with a strain dropdown.

    If `description_keywords` is set (either via config override or a subclass),
    new-strain alerts are filtered: only fire if that strain's section of the
    short-description block contains one of the keywords (case-insensitive).
    """
    name = ""
    label = ""
    description_keywords = []

    VARIATIONS_RE = re.compile(
        r'data-product_variations="([^"]*)"', re.DOTALL
    )
    SHORT_DESC_RE = re.compile(
        r'<div[^>]*class="[^"]*woocommerce-product-details__short-description[^"]*"[^>]*>'
        r'(.*?)</div>',
        re.DOTALL,
    )

    def __init__(self, cfg):
        self.cfg = cfg
        if cfg.get("description_keywords") is not None:
            self.description_keywords = [
                k.lower() for k in cfg["description_keywords"]
            ]

    # A strain heading is a <strong> that OPENS its own paragraph. Bold text
    # used mid-sentence (the page carries <strong>"Top Tier"</strong> inside
    # the intro paragraph) is not a heading and must never end a description.
    HEADING_RE = re.compile(
        r'<p[^>]*>\s*<strong>(.*?)</strong>', re.DOTALL | re.IGNORECASE
    )
    ANY_STRONG_RE = re.compile(r'<strong>(.*?)</strong>', re.DOTALL | re.IGNORECASE)

    @staticmethod
    def _clean(fragment):
        text = re.sub(r"<[^>]+>", " ", fragment)
        text = htmllib.unescape(text)
        text = text.replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _match_strain(cls, heading_text, strains):
        """Which dropdown strain, if any, does this heading introduce?

        Headings carry genetics the dropdown does not ("Soul Assassin" ->
        "Soul Assassin OG (Sour Diesel x OG Kush Pheno)"), so match on prefix
        with a word boundary. Longest match wins so a strain whose name is a
        prefix of another cannot steal the heading.
        """
        h = heading_text.lower()
        best = None
        for strain in strains:
            s = strain.lower()
            if not s or not h.startswith(s):
                continue
            if len(h) > len(s) and h[len(s)].isalnum():
                continue  # mid-word: "M1 OG" must not match a "M1 OGX" heading
            if best is None or len(strain) > len(best):
                best = strain
        return best

    @classmethod
    def _extract_descriptions(cls, html_text, strains):
        m = cls.SHORT_DESC_RE.search(html_text)
        if not m:
            return {}
        block = m.group(1)
        # EVERY paragraph-leading heading is a boundary, not only the ones for
        # strains currently in the dropdown. A strain that sells out is dropped
        # from the dropdown but its heading stays on the page; if such an
        # orphan does not terminate the preceding chunk, the preceding strain
        # absorbs its write-up. That is exactly how "Snozzberries" swallowed
        # G-Chem's "10/10" and fired a false NEW_STRAIN alert on 2026-08-25.
        boundaries = [
            (hm.start(), hm.end(), cls._clean(hm.group(1)))
            for hm in cls.HEADING_RE.finditer(block)
        ]
        if len(boundaries) < 2:
            # Markup changed shape; fall back to treating any <strong> as a
            # boundary rather than emitting one giant merged description.
            boundaries = [
                (hm.start(), hm.end(), cls._clean(hm.group(1)))
                for hm in cls.ANY_STRONG_RE.finditer(block)
            ]
        boundaries.sort()
        descs = {}
        for i, (h_start, h_end, h_text) in enumerate(boundaries):
            strain = cls._match_strain(h_text, strains)
            if strain is None:
                continue  # orphan or non-strain heading: acts as a boundary only
            nxt = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(block)
            text = cls._clean(block[h_end:nxt])
            if strain not in descs or len(text) > len(descs[strain]):
                descs[strain] = text
        return descs

    def fetch(self, session, ua, timeout):
        # expect_pattern: the product page is worthless without the variations
        # JSON, and a WAF can withhold it under a 200. Declaring what the page
        # must contain lets http_get retry with a browser fingerprint instead
        # of failing with a parse error that looks like a site redesign.
        resp = http_get(session, self.cfg["url"], ua, timeout,
                        expect_pattern=self.VARIATIONS_RE)
        html_text = resp.text
        m = self.VARIATIONS_RE.search(html_text)
        if not m:
            raise RuntimeError("variations JSON not found on page")
        variants = json.loads(htmllib.unescape(m.group(1)))
        strains = set()
        for v in variants:
            attrs = v.get("attributes", {}) or {}
            strain = (
                attrs.get("attribute_strain")
                or attrs.get("attribute_pa_strain")
                or ""
            ).strip()
            if strain:
                strains.add(strain)
        strains = sorted(strains)
        descriptions = self._extract_descriptions(html_text, strains)
        return {"strains": strains, "descriptions": descriptions}

    def diff(self, prev_state, current):
        now_iso = datetime.now(timezone.utc).isoformat()
        prev_strains = set(prev_state.get("strains", []))
        curr_strains = set(current.get("strains", []))
        descriptions = current.get("descriptions", {})
        # Preserve all previously-seen strains so one falling off the dropdown
        # does not re-alert later.
        merged_descriptions = dict(prev_state.get("descriptions", {}))
        if descriptions:
            # The parse produced results, so it is authoritative for every
            # strain currently on the page. Drop stale text for on-page
            # strains rather than merging over it: a description captured by
            # an older, buggy parser (one that absorbed the next strain's
            # write-up) would otherwise survive the fix and keep matching
            # forever, and a dropdown strain whose heading is missing from the
            # page would keep matching on text that is not its own.
            # An empty parse means the page or its markup failed to load, so
            # in that case keep every prior description untouched.
            for strain in curr_strains:
                merged_descriptions.pop(strain, None)
        merged_descriptions.update(descriptions)

        is_first_run = not prev_strains
        # `alerted` = strains we have already fired an alert for. We re-check
        # every not-yet-alerted strain's description on EVERY run, not only the
        # run it first appears, because the vendor frequently adds a strain to
        # the dropdown minutes before its description (which carries the
        # keyword) is written onto the page. Checking only at first appearance
        # silently dropped those (e.g. "Diesel Burger", 2026-06-16). The
        # ABSENCE of this key marks pre-tracking state: seed it to the full
        # current catalog so the upgrade starts clean -- no retro alerts for
        # strains already on the page.
        migrating = "alerted" not in prev_state
        alerted = set(prev_state.get("alerted", []))

        alerts = []
        if is_first_run or migrating:
            alerted |= curr_strains
        else:
            for strain in sorted(curr_strains - alerted):
                if self.description_keywords:
                    if not descriptions:
                        # The description block did not parse this run, so we
                        # have no fresh evidence about any strain. Matching
                        # against stored text here would let a stale or
                        # previously mis-parsed description fire an alert on a
                        # run that actually read nothing. Wait for a good run.
                        continue
                    desc_lower = merged_descriptions.get(strain, "").lower()
                    matched = next(
                        (kw for kw in self.description_keywords if kw in desc_lower),
                        None,
                    )
                    if not matched:
                        # Keep watching; the description may match on a later
                        # run once the vendor fills it in.
                        continue
                    detail = f'matched keyword "{matched}"'
                else:
                    detail = "added to strain dropdown"
                alerts.append({
                    "site": self.name, "label": self.label,
                    "kind": "NEW_STRAIN", "title": strain,
                    "url": self.cfg["url"], "details": detail,
                })
                alerted.add(strain)

        new_state = {
            "strains": sorted(prev_strains | curr_strains),
            "descriptions": merged_descriptions,
            "alerted": sorted(alerted),
            "last_seen": now_iso,
        }
        return new_state, alerts


class HempBarnLivingSoilSite(HempBarnSite):
    name = "hempbarn_livingsoil"
    label = "Hemp Barn — Living Soil"
    description_keywords = ["special", "all time", "all-time", "10/10"]


class HempBarnOrganicSoilSite(HempBarnSite):
    name = "hempbarn_organicsoil"
    label = "Hemp Barn — Organic Soil"
    description_keywords = ["special", "all time", "all-time", "10/10"]


class CaregiverPharmsSite:
    name = "caregiverpharms"
    label = "Caregiver Pharms"

    def __init__(self, cfg):
        self.cfg = cfg
        self.smalls_keywords = [k.lower() for k in cfg.get("smalls_keywords", [])]

    def _is_smalls_variant(self, variant_title):
        title_lower = (variant_title or "").lower()
        return any(kw in title_lower for kw in self.smalls_keywords)

    def fetch(self, session, ua, timeout):
        resp = http_get(session, self.cfg["products_json_url"], ua, timeout, expect_json=True)
        data = resp.json()
        out = {}
        for p in data.get("products", []):
            variants = p.get("variants", []) or []
            any_available = any(v.get("available", False) for v in variants)
            smalls_variant_present = False
            smalls_variant_available = False
            variant_map = {}
            for v in variants:
                if self._is_smalls_variant(v.get("title")):
                    smalls_variant_present = True
                    if v.get("available", False):
                        smalls_variant_available = True
                vtitle = (v.get("title") or "").strip() or "Default"
                variant_map[vtitle] = bool(v.get("available", False))
            out[p["handle"]] = {
                "title": p.get("title", p["handle"]),
                "any_available": any_available,
                "smalls_present": smalls_variant_present,
                "smalls_available": smalls_variant_available,
                # Per-variant availability. `any_available` alone cannot detect
                # a restock on a product that keeps one slow-moving variant
                # (e.g. the $265 Baller Ounce) permanently in stock: the
                # aggregate never goes False, so it can never transition back
                # to True. Blueberry Chem #23 was invisible for exactly this
                # reason on 2026-08-06.
                "variants": variant_map,
            }
        return out

    @staticmethod
    def _norm_variant(title):
        """Normalise a variant title for never-seen-before comparison.

        The shop writes the same size inconsistently -- both "1 oz (28g)
        Smalls and Micros" and "1 Oz. (28g) Smalls and Micros" are on record.
        Without normalising, a casing or punctuation edit would read as a
        brand-new size and false-alert. Abbreviation periods are dropped but a
        decimal point is kept, so "1/8 Oz. (3.5 Grams)" does not become
        "(35 grams)".
        """
        t = (title or "").strip().lower()
        t = re.sub(r"(?<!\d)\.(?!\d)", "", t)
        return re.sub(r"\s+", " ", t).strip()

    def diff(self, prev_state, current):
        """Two alerts only, per the 2026-09-10 narrowing:

          SMALLS_BACK  - an ounce of smalls becomes available on any product
          NEW_VARIANT  - a size option appears that this shop has NEVER
                         offered on any product

        Everything else is deliberately silent. Ordinary restocks of existing
        sizes are noise, and a new STRAIN is explicitly not wanted: only a
        size that never existed before counts as new here.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        prev = prev_state.get("products", {})
        alerts = []
        product_url_base = "https://caregiverpharms.com/products/"

        known = set(prev_state.get("known_variant_titles", []))
        seeding_vocabulary = "known_variant_titles" not in prev_state
        if seeding_vocabulary:
            # First run under this rule. Seed the vocabulary from every variant
            # ever recorded (state preserves products that leave the catalog),
            # so the switchover does not alert on the entire existing size list.
            for was in prev.values():
                for vtitle in (was.get("variants") or {}):
                    known.add(self._norm_variant(vtitle))

        first_sighting = {}
        for handle, p in sorted(current.items()):
            for vtitle in p.get("variants", {}):
                norm = self._norm_variant(vtitle)
                if norm not in known and norm not in first_sighting:
                    first_sighting[norm] = (vtitle, p["title"], handle)

        if not seeding_vocabulary:
            for norm, (vtitle, ptitle, handle) in sorted(first_sighting.items()):
                alerts.append({
                    "site": self.name, "label": self.label,
                    "kind": "NEW_VARIANT", "title": vtitle,
                    "url": product_url_base + handle,
                    "details": "size never offered before; first seen on %s" % ptitle,
                })
        known |= set(first_sighting)

        for handle, p in sorted(current.items()):
            was = prev.get(handle)
            if was is None:
                # A brand-new strain is NOT an alert under these rules. Record
                # it so its smalls transitions are tracked from here on.
                continue
            if p["smalls_available"] and not was.get("smalls_available", False):
                alerts.append({
                    "site": self.name, "label": self.label,
                    "kind": "SMALLS_BACK", "title": p["title"],
                    "url": product_url_base + handle,
                    "details": "ounce of smalls/micros available",
                })

        # Preserve previously-seen handles to prevent re-alerts when a
        # product temporarily disappears from the collection JSON.
        merged_products = dict(prev)
        for handle, p in current.items():
            merged_products[handle] = dict(p)
        new_state = {
            "products": merged_products,
            "known_variant_titles": sorted(known),
            "last_seen": now_iso,
        }
        return new_state, alerts


class FlowGardensSmallsSite:
    """Flow Gardens smalls, watched across the whole catalog.

    Until 2026-08-12 the shop sold ONE product, handle `smalls`, whose Strain
    dropdown carried the type inside each strain name, so this adapter watched
    that single handle's options. At 2026-08-12 13:49 EDT the shop split it
    into one product per type -- "Type 1 (THCa) Smallz", "Type 3 (CBD)
    Smallz", "Type 4 (CBG) Smallz" -- and deleted the old handle. The last
    successful poll was 13:40 EDT that day; every poll after it 404ed, and the
    site went unmonitored for 28 days.

    Two things changed. The type now lives in the PRODUCT title rather than
    the strain name, and a watched type may have no product at all (there was
    no Type 2 Smallz on 2026-09-09). So watching one hard-coded handle cannot
    work: the thing being waited for is a product that does not exist yet.
    Scan the catalog instead, and alert both when a smalls product of a
    watched type is listed and when a new strain is added to one.
    """
    name = "flowgardens_smalls"
    label = "Flow Gardens — Smalls"

    # \b prevents Type 2 matching "Type 20" if the merchant ever uses two digits.
    TYPE_RE = re.compile(r"\btype\s*[-_ ]?(\d+)\b", re.IGNORECASE)
    # "smalls"/"smallz" only. Must NOT match the separate "Small Batch" line or
    # "Assorted Small Buds", which are different products entirely.
    SMALLS_RE = re.compile(r"small[sz]\b", re.IGNORECASE)

    def __init__(self, cfg):
        self.cfg = cfg
        at = cfg.get("allowed_types")
        self.allowed_types = set(int(x) for x in at) if at else None
        self.products_json_url = cfg.get(
            "products_json_url", "https://flowgardens.com/products.json?limit=250")
        self.shop_url = cfg.get("shop_url", "https://flowgardens.com/collections/all")

    @classmethod
    def _product_type(cls, *texts):
        for text in texts:
            m = cls.TYPE_RE.search(text or "")
            if m:
                return int(m.group(1))
        return None

    @classmethod
    def _is_smalls(cls, handle, title):
        return bool(cls.SMALLS_RE.search(handle or "") or cls.SMALLS_RE.search(title or ""))

    def fetch(self, session, ua, timeout):
        resp = http_get(session, self.products_json_url, ua, timeout, expect_json=True)
        out = {}
        for p in resp.json().get("products", []) or []:
            handle = p.get("handle")
            title = p.get("title", "") or ""
            if not handle or not self._is_smalls(handle, title):
                continue
            strains = set()
            for v in p.get("variants", []) or []:
                opt1 = v.get("option1")
                if opt1:
                    strains.add(opt1.strip())
            for opt in p.get("options", []) or []:
                if (opt.get("name") or "").lower() == "strain":
                    for value in opt.get("values", []) or []:
                        strains.add(value.strip())
            out[handle] = {
                "title": title,
                "type": self._product_type(title, handle),
                "strains": sorted(strains),
            }
        return {"products": out}

    def _watched(self, product):
        return (self.allowed_types is None
                or product.get("type") in self.allowed_types)

    def diff(self, prev_state, current):
        now_iso = datetime.now(timezone.utc).isoformat()
        prev = prev_state.get("products")
        # `products` absent means state predates the catalog-wide rewrite (it
        # held a flat `strains` list for the old single handle). Seed silently:
        # every smalls product on the site would otherwise alert at once.
        migrating = prev is None
        prev = prev or {}
        curr = current.get("products", {})
        alerts = []

        if not migrating:
            for handle, p in sorted(curr.items()):
                if not self._watched(p):
                    logging.info("[%s] %s suppressed: type=%s not in %s",
                                 self.name, handle, p.get("type"),
                                 sorted(self.allowed_types or []))
                    continue
                url = "https://flowgardens.com/products/" + handle
                was = prev.get(handle)
                if was is None:
                    alerts.append({
                        "site": self.name, "label": self.label,
                        "kind": "NEW_PRODUCT", "title": p["title"],
                        "url": url,
                        "details": "Type %s smalls product listed (%d strain(s))"
                                   % (p.get("type"), len(p["strains"])),
                    })
                    continue
                new_strains = [s for s in p["strains"]
                               if s not in set(was.get("strains", []))]
                if new_strains:
                    alerts.append({
                        "site": self.name, "label": self.label,
                        "kind": "NEW_STRAIN", "title": p["title"],
                        "url": url,
                        "details": "new strain(s): " + ", ".join(sorted(new_strains)),
                    })

        # Preserve products and their strains so one dropping off the catalog
        # does not re-alert on its return.
        merged = {h: dict(v) for h, v in prev.items()}
        for handle, p in curr.items():
            keep = merged.get(handle, {})
            strains = sorted(set(keep.get("strains", [])) | set(p["strains"]))
            merged[handle] = {"title": p["title"], "type": p["type"], "strains": strains}
        return {"products": merged, "last_seen": now_iso}, alerts


class FiveLeafWellnessSite:
    """WooCommerce shop monitored via the WooCommerce Store API.

    Alerts when a product in one of the target categories (default: the
    "top-shelf" tier) newly appears. Tier membership is read from each
    product's authoritative WooCommerce ``categories`` -- not by scraping
    page text. The previous adapter inferred the tier from the words "top
    shelf" appearing in specific page modules, but a product's tier renders
    only as a category link (which the scan deliberately skipped so the nav
    menu would not false-match), and products were never re-checked after
    first sighting. So a product moved into Top Shelf *after* it first
    appeared -- or that dropped before being categorised -- was missed
    forever. Reading categories from the API fixes both failure modes.
    """
    name = "fiveleafwellness"
    label = "Five Leaf Wellness"

    def __init__(self, cfg):
        self.cfg = cfg
        self.api_url = cfg.get(
            "products_api_url",
            "https://fiveleafwellness.com/wp-json/wc/store/v1/products",
        )
        self.shop_url = cfg.get("shop_url", "https://fiveleafwellness.com/shop/")
        # Category slugs that trigger an alert. The site's top tier is the
        # "top-shelf" category; "mid-tier" is lower and intentionally excluded.
        # (There is no "top-tier" category on this site -- it was a synonym in
        # the old keyword list that matched nothing.)
        self.target_categories = [
            c.lower().strip() for c in cfg.get("target_categories", ["top-shelf"])
        ]

    def fetch(self, session, ua, timeout):
        products = {}
        page = 1
        while True:
            url = f"{self.api_url}?per_page=100&page={page}"
            resp = http_get(session, url, ua, timeout, expect_json=True)
            batch = resp.json()
            if not isinstance(batch, list):
                raise RuntimeError("store API did not return a product list")
            for p in batch:
                slug = p.get("slug")
                if not slug:
                    continue
                products[slug] = {
                    "name": (p.get("name") or slug).strip(),
                    "permalink": p.get("permalink") or self.shop_url,
                    "categories": [
                        (c.get("slug") or "").lower()
                        for c in p.get("categories", [])
                    ],
                    "in_stock": bool(p.get("is_in_stock", True)),
                }
            try:
                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                break
            page += 1
            time.sleep(0.4)
        if not products:
            # Treat an empty catalog as a transient failure rather than wiping
            # state -- re-seeding would silently drop real future alerts.
            raise RuntimeError(
                "store API returned no products; format may have changed"
            )
        return {"products": products}

    def _targets_of(self, info):
        return [c for c in info.get("categories", []) if c in self.target_categories]

    def diff(self, prev_state, current):
        now_iso = datetime.now(timezone.utc).isoformat()
        products = current.get("products", {})
        curr_target = {
            slug for slug, info in products.items() if self._targets_of(info)
        }

        # `alerted` = slugs already alerted as a target-category product. Tier
        # membership is recomputed every run, so a product moved into Top Shelf
        # after it first appeared (or that dropped before being categorised)
        # still alerts exactly once. Absence of the key marks pre-upgrade
        # state: seed the current target members as already-handled (start
        # clean) and carry forward anything the old keyword schema alerted, so
        # the switch never re-fires on the existing catalog.
        migrating = "alerted" not in prev_state
        alerted = set(prev_state.get("alerted", []))
        if migrating:
            for url, v in prev_state.get("products", {}).items():
                if isinstance(v, dict) and v.get("matched_kw"):
                    alerted.add(v.get("slug") or url)

        alerts = []
        if migrating:
            alerted |= curr_target
        else:
            for slug in sorted(curr_target - alerted):
                info = products[slug]
                tier = self._targets_of(info)[0].replace("-", " ").title()
                stock = "in stock" if info["in_stock"] else "currently sold out"
                alerts.append({
                    "site": self.name, "label": self.label,
                    "kind": "NEW_PRODUCT", "title": info["name"],
                    "url": info["permalink"],
                    "details": f"new {tier} product ({stock})",
                })
                alerted.add(slug)

        new_state = {
            "alerted": sorted(alerted),
            "target_current": sorted(curr_target),
            "last_seen": now_iso,
        }
        return new_state, alerts


class BeleaferIndoorSite:
    """WooCommerce category page (Gutenberg/block theme). On each new product,
    fetches its detail page, isolates the WooCommerce 'product-summary' block,
    and alerts only if the description text contains a 'Type N' designation
    matching `allowed_types`.
    """
    name = "beleafer_indoor"
    label = "Beleafer — Indoor Hemp Flower"

    PRODUCT_URL_RE = re.compile(
        r'href="(https://beleafer\.com/product/[a-z0-9-]+/?)"'
    )
    SLUG_RE = re.compile(
        r"https://beleafer\.com/product/([a-z0-9-]+)"
    )
    SUMMARY_BLOCK_RE = re.compile(
        r'<div[^>]*data-block-name="woocommerce/product-summary"[^>]*>'
        r'([\s\S]*?)'
        r'(?=<div[^>]*data-block-name="woocommerce/|<div class="wp-block-|</body>|\Z)',
        re.IGNORECASE,
    )
    TYPE_RE = re.compile(r"\btype\s*(\d+)\b", re.IGNORECASE)

    def __init__(self, cfg):
        self.cfg = cfg
        self.category_url = cfg["category_url"]
        at = cfg.get("allowed_types")
        self.allowed_types = set(int(x) for x in at) if at else None
        self.max_pages = cfg.get("max_pages", 20)
        # The shop's host refuses plain `requests` every time, so open with a
        # browser fingerprint instead of a request it already scores as a bot.
        self.impersonate = bool(cfg.get("impersonate", False))
        self._session = None
        self._ua = None
        self._timeout = None

    def _slug_of(self, url):
        m = self.SLUG_RE.match(url)
        return m.group(1) if m else url

    def _fetch_product_page(self, url):
        try:
            resp = http_get(self._session, url, self._ua, self._timeout,
                            impersonate=self.impersonate)
            return resp.text
        except Exception as exc:
            logging.warning("[%s] failed to fetch %s: %s", self.name, url, exc)
            return None

    def _types_in_summary(self, page_html):
        """Return the set of integer type numbers that appear in the
        product-summary block. Empty set if the block is missing."""
        if not page_html:
            return set()
        m = self.SUMMARY_BLOCK_RE.search(page_html)
        if not m:
            return set()
        block = m.group(1)
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ",
                      block, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = htmllib.unescape(text)
        return set(int(t) for t in self.TYPE_RE.findall(text))

    def fetch(self, session, ua, timeout):
        self._session = session
        self._ua = ua
        self._timeout = timeout
        urls = set()
        base = self.category_url.rstrip("/")
        for page in range(1, self.max_pages + 1):
            page_url = self.category_url if page == 1 else f"{base}/page/{page}/"
            try:
                # expect_pattern on page 1 only: a first page with no products
                # is always wrong, but a LATER page having none is how
                # pagination ends. A WAF can strip the listing under a 200,
                # which is how Beleafer regressed 2026-09-09 23:25 UTC.
                resp = http_get(session, page_url, ua, timeout,
                                expect_pattern=self.PRODUCT_URL_RE if page == 1 else None,
                                impersonate=self.impersonate)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                # A 404 past page 1 means "no more pages". A 404 on page 1
                # means the listing URL itself is gone, which is a FAILURE and
                # must be raised so the health counter sees it. Breaking here
                # instead returned an empty result set that looked like a clean
                # poll: High Alpine's Weebly search URL started 404ing after
                # the shop moved to Shopify, and the site sat silently
                # unmonitored from 2026-06-19 to 2026-09-09 -- 82 days, no
                # exception, no alert, last_polled_at refreshing every minute.
                if status == 404 and page > 1:
                    break
                raise
            page_urls = set(self.PRODUCT_URL_RE.findall(resp.text))
            new = page_urls - urls
            if not new:
                break
            urls |= new
            time.sleep(0.4)
        if not urls:
            # A storefront category that parses to zero products is never
            # legitimate here; it means the markup or the URL changed. Raising
            # routes it to the health counter instead of silently reporting an
            # empty catalog, which diffs to "no change" forever.
            raise RuntimeError(
                "no products parsed from %s; listing markup or URL may have changed"
                % self.category_url)
        return {"product_urls": sorted(urls)}

    def _evaluate(self, url):
        slug = self._slug_of(url)
        html_text = self._fetch_product_page(url)
        types_found = self._types_in_summary(html_text)
        if self.allowed_types is None:
            matching = sorted(types_found)
        else:
            matching = sorted(types_found & self.allowed_types)
        record = {"slug": slug, "types": sorted(types_found)}
        time.sleep(0.3)
        if matching:
            display = slug.replace("-", " ").title()
            kinds = ", ".join(f"Type {t}" for t in matching)
            alert = {
                "site": self.name, "label": self.label,
                "kind": "NEW_PRODUCT", "title": display,
                "url": url,
                "details": f"description mentions {kinds}",
            }
            return True, record, alert
        return False, record, None

    def diff(self, prev_state, current):
        # Re-check each not-yet-matched product's detail page every run (until
        # it matches or the window lapses), so a "Type 2" designation added
        # AFTER the product is first listed is not missed.
        curr_urls = set(current.get("product_urls", []))
        return recheck_listing_diff(
            prev_state, curr_urls, datetime.now(timezone.utc),
            evaluate=self._evaluate,
            window_hours=self.cfg.get(
                "recheck_window_hours", DEFAULT_RECHECK_WINDOW_HOURS),
        )


class HighAlpineGeneticsSite:
    """Weebly-based shop that mixes seeds and flower in one search listing.
    Alerts only when (a) the product is NOT a seed AND (b) the product name
    OR description contains "Type N" matching `allowed_types`.
    """
    name = "highalpinegenetics"
    label = "High Alpine Genetics"

    SEARCH_URL = "https://www.highalpinegenetics.com/apps/search"
    BASE_URL = "https://www.highalpinegenetics.com"

    RESULT_RE = re.compile(
        r'<li class="wsite-search-product-result">\s*'
        r'<a href="([^"]+)"[^>]*>[\s\S]*?'
        r'<span class="wsite-search-product-name" title="[^"]*">([^<]+)</span>'
        r'[\s\S]*?</li>',
        re.IGNORECASE,
    )
    DESC_RE = re.compile(
        r'<div[^>]*itemprop="description"[^>]*>([\s\S]*?)</div>',
        re.IGNORECASE,
    )
    TYPE_RE = re.compile(r'\btype\s*(?:#)?(\d+)\b', re.IGNORECASE)
    SEED_NAME_RE = re.compile(r'\b(seed|seeds|fem|feminized)\b', re.IGNORECASE)
    # Phrases that unambiguously indicate a seed / cultivation product even
    # when the product name does not say "seed" explicitly. Kept tight on
    # purpose: terms like "f1 hybrid", "bred by", "phenotype" also appear
    # in normal flower marketing copy, so they are NOT in this list.
    # Tunable via config["seed_phrases"].
    DEFAULT_SEED_DESC_PHRASES = (
        "flowering time", "weeks flowering", "germinat",
    )
    # Phrases that signal a concentrate / extract product. If any of these
    # appear in the description, we do NOT treat the product as a seed even
    # when the seed_phrases above also match. Tunable via config["non_seed_overrides"].
    DEFAULT_NON_SEED_OVERRIDES = ("rosin",)

    def __init__(self, cfg):
        self.cfg = cfg
        self.search_url = cfg.get("search_url", f"{self.SEARCH_URL}?q=+")
        at = cfg.get("allowed_types") or [1, 2]
        self.allowed_types = set(int(x) for x in at)
        self.max_pages = cfg.get("max_pages", 30)
        sp = cfg.get("seed_phrases")
        self.seed_phrases = tuple(p.lower() for p in sp) if sp else self.DEFAULT_SEED_DESC_PHRASES
        ns = cfg.get("non_seed_overrides")
        self.non_seed_overrides = tuple(p.lower() for p in ns) if ns else self.DEFAULT_NON_SEED_OVERRIDES
        self._session = None
        self._ua = None
        self._timeout = None

    def fetch(self, session, ua, timeout):
        self._session = session
        self._ua = ua
        self._timeout = timeout
        results = {}
        # Strip any existing &page= from the configured URL.
        base = re.sub(r"&page=\d+", "", self.search_url)
        sep = "&" if "?" in base else "?"
        for page in range(1, self.max_pages + 1):
            page_url = base if page == 1 else f"{base}{sep}page={page}"
            try:
                # expect_pattern on page 1 only: a first page with no products
                # is always wrong, but a LATER page having none is how
                # pagination ends. A WAF can strip the listing under a 200,
                # which is how Beleafer regressed 2026-09-09 23:25 UTC.
                resp = http_get(session, page_url, ua, timeout,
                                expect_pattern=self.RESULT_RE if page == 1 else None)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                # A 404 past page 1 means "no more pages". A 404 on page 1
                # means the listing URL itself is gone, which is a FAILURE and
                # must be raised so the health counter sees it. Breaking here
                # instead returned an empty result set that looked like a clean
                # poll: High Alpine's Weebly search URL started 404ing after
                # the shop moved to Shopify, and the site sat silently
                # unmonitored from 2026-06-19 to 2026-09-09 -- 82 days, no
                # exception, no alert, last_polled_at refreshing every minute.
                if status == 404 and page > 1:
                    break
                raise
            page_results = self._parse_results(resp.text)
            if not page_results:
                break
            fresh = {u: n for u, n in page_results.items() if u not in results}
            if not fresh:
                break
            results.update(fresh)
            time.sleep(0.4)
        if not results:
            # See BeleaferIndoorSite.fetch: an empty parse is a failure, not an
            # empty shop. This is the exact condition that hid High Alpine's
            # platform migration for 82 days.
            raise RuntimeError(
                "no products parsed from %s; listing markup or URL may have changed"
                % self.search_url)
        return {"results": results}

    @classmethod
    def _parse_results(cls, html_text):
        out = {}
        for m in cls.RESULT_RE.finditer(html_text):
            href = m.group(1)
            name = htmllib.unescape(m.group(2)).strip()
            if href.startswith("/"):
                href = cls.BASE_URL + href
            out[href] = name
        return out

    def _fetch_detail(self, url):
        try:
            resp = http_get(self._session, url, self._ua, self._timeout)
            return resp.text
        except Exception as exc:
            logging.warning("[%s] failed to fetch %s: %s", self.name, url, exc)
            return None

    def _description_text(self, html_text):
        if not html_text:
            return ""
        m = self.DESC_RE.search(html_text)
        if not m:
            return ""
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ",
                      m.group(1), flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = htmllib.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    def _is_seed(self, name, desc_text):
        # Name-based "seed/seeds/fem/feminized" check is definitive and is
        # not overridden by description signals.
        if self.SEED_NAME_RE.search(name or ""):
            return True
        desc_l = (desc_text or "").lower()
        # Concentrate override: description signals an extract product (e.g.,
        # "rosin"). Skip the description-based seed phrases that could
        # otherwise false-positive when describing source plant material.
        if any(p in desc_l for p in self.non_seed_overrides):
            return False
        if any(p in desc_l for p in self.seed_phrases):
            return True
        if len(re.findall(r"\bseeds?\b", desc_l)) >= 3:
            return True
        return False

    def _types_in(self, text):
        return set(int(t) for t in self.TYPE_RE.findall(text or ""))

    def diff(self, prev_state, current):
        results = current.get("results", {})
        curr_urls = set(results.keys())

        def _evaluate(url):
            name = results.get(url, "")
            html_text = self._fetch_detail(url)
            desc_text = self._description_text(html_text)
            is_seed = self._is_seed(name, desc_text)
            types = self._types_in(name) | self._types_in(desc_text)
            record = {
                "name": name, "is_seed": is_seed,
                "matched_types": sorted(types),
            }
            time.sleep(0.3)
            if is_seed:
                return False, record, None
            matched = types & self.allowed_types
            if matched:
                kinds = ", ".join(f"Type {t}" for t in sorted(matched))
                alert = {
                    "site": self.name, "label": self.label,
                    "kind": "NEW_PRODUCT", "title": name,
                    "url": url, "details": kinds,
                }
                return True, record, alert
            return False, record, None

        # Re-check each not-yet-matched product every run (until it matches or
        # the window lapses) so a type designation that the vendor adds to the
        # name/description after first listing the product is still caught.
        return recheck_listing_diff(
            prev_state, curr_urls, datetime.now(timezone.utc),
            evaluate=_evaluate,
            window_hours=self.cfg.get(
                "recheck_window_hours", DEFAULT_RECHECK_WINDOW_HOURS),
        )


class AntheiaFarmsSite:
    """Shopify collection (antheiafarms.myshopify.com). Alerts when a product
    becomes purchasable: an existing product's variant going sold-out ->
    available (a restock / drop), or a brand-new product appearing already in
    stock. Everything is sold out ahead of the scheduled drop, so the first
    run only seeds and an availability transition is what fires later. A new
    product that appears sold out is recorded silently and alerts when it
    later flips available.
    """
    name = "antheiafarms"
    label = "Antheia Farms"

    def __init__(self, cfg):
        self.cfg = cfg
        self.product_url_base = cfg.get(
            "product_url_base", "https://antheiafarms.myshopify.com/products/")

    def fetch(self, session, ua, timeout):
        resp = http_get(session, self.cfg["products_json_url"], ua, timeout,
                        expect_json=True)
        out = {}
        for p in resp.json().get("products", []):
            handle = p.get("handle")
            if not handle:
                continue
            variants = p.get("variants", []) or []
            variant_map = {}
            for v in variants:
                vtitle = (v.get("title") or "").strip() or "Default"
                variant_map[vtitle] = bool(v.get("available", False))
            out[handle] = {
                "title": p.get("title", handle),
                "any_available": any(v.get("available", False) for v in variants),
                # See CaregiverPharmsSite.fetch: an aggregate availability flag
                # misses a restock whenever one variant stays in stock.
                "variants": variant_map,
            }
        return out

    def diff(self, prev_state, current):
        now_iso = datetime.now(timezone.utc).isoformat()
        prev = prev_state.get("products", {})
        is_first_run = not prev
        alerts = []
        for handle, p in current.items():
            url = self.product_url_base + handle
            was = prev.get(handle)
            if was is None:
                # New product: alert only if it is already in stock (a fresh
                # in-stock drop). A new sold-out product is recorded and will
                # alert when it flips available. Never alert on the seed run.
                if not is_first_run and p["any_available"]:
                    alerts.append({
                        "site": self.name, "label": self.label,
                        "kind": "DROP", "title": p["title"],
                        "url": url, "details": "new product, in stock",
                    })
                continue
            prev_variants = was.get("variants")
            if prev_variants is None:
                # Predates per-variant tracking: seed silently this run.
                continue
            newly_available = [
                vtitle for vtitle, avail in p["variants"].items()
                if avail and not prev_variants.get(vtitle, False)
            ]
            if newly_available:
                alerts.append({
                    "site": self.name, "label": self.label,
                    "kind": "RESTOCK", "title": p["title"],
                    "url": url,
                    "details": "back in stock: " + ", ".join(newly_available),
                })
        # Preserve previously-seen handles so a product temporarily missing
        # from the feed does not re-alert when it returns.
        merged = dict(prev)
        for handle, p in current.items():
            merged[handle] = dict(p)
        return {"products": merged, "last_seen": now_iso}, alerts


class VeritasSite:
    """VERITAS (veritasthca.com): a WooCommerce wholesale shop plus a curated
    "drop menu" page. Alerts on exactly two things, per the brief of
    2026-10-13, and on nothing else:

      ROSIN_5G   rosin or hash that can be bought in 5 g or less
      EXOTIC_OZ  an ounce (zip) of flower the vendor labels "Exotic"

    Silent by design: per-gram wholesale rosin with a 10 g / 28 g MOQ (the
    entire rosin menu on 2026-10-13), pounds and QPs, sugar and live resin,
    vapes including rosin disposables, edibles including the hash/rosin
    chocolates announced for 10/15, and prerolls including hash-infused ones.

    Two sources, because each misses things the other has:
      * The WooCommerce Store API is the ground truth for what can be bought
        right now. The menu is partly stale: on 2026-10-13 seven of its links
        pointed at deleted products (404) and four at sold-out ones, so a menu
        line that links to a product counts only while the store lists that
        product as in stock.
      * The menu page carries the vendor's own wording ("10g MOQ", "per oz"),
        production notes, and lots that have no store product at all.

    Never read the cart minimum as the MOQ. Most rosin listings accept a
    quantity of 1 (`add_to_cart.minimum` 1, the WooCommerce default) while the
    menu prices the same product "$27.00 per gram | 10g MOQ", and checkout
    refuses anything under that MOQ (confirmed first-hand 2026-10-13). Reading
    1 as "sold by the gram" would have alerted on 33 listings on day one. A
    minimum of 1 is no evidence either way; only a stated size or MOQ is.

    Every rule is re-evaluated from fresh data on every run, so a size option
    or wording added after a product first appears still fires.
    """
    name = "veritas"
    label = "VERITAS"

    MENU_URL = "https://veritasthca.com/veritas-drop-menu/"
    API_URL = "https://veritasthca.com/wp-json/wc/store/v1/products"
    # Descriptions are most of the payload, but they are where the vendor
    # writes jar sizes and MOQs ("2 Gram Jar", "MOQ 1"), so they stay in.
    API_FIELDS = ("id,name,slug,permalink,type,categories,tags,is_in_stock,"
                  "is_purchasable,add_to_cart,attributes,variations,"
                  "short_description,description,prices")

    # ---- page structure (Elementor) ----
    PRICE_LIST_RE = re.compile(r'class="elementor-price-list-item"')
    LI_RE = re.compile(r"<li\b([^>]*)>(.*?)</li>", re.DOTALL | re.IGNORECASE)
    TITLE_RE = re.compile(r'elementor-price-list-title"[^>]*>(.*?)</span>', re.DOTALL)
    PRICE_RE = re.compile(r'elementor-price-list-price"[^>]*>(.*?)</span>', re.DOTALL)
    DESC_RE = re.compile(r'elementor-price-list-description"[^>]*>(.*?)</p>', re.DOTALL)
    HREF_RE = re.compile(r'href="([^"]*)"')
    IMAGE_WIDGET_RE = re.compile(r'data-widget_type="image\.default".*?(<img\b[^>]*>)', re.DOTALL)
    HEADING_RE = re.compile(r'class="elementor-heading-title[^"]*"[^>]*>(.*?)</h[1-6]>', re.DOTALL)
    CARD_RE = re.compile(r'<article\s+class="vb-card"(.*?)</article>', re.DOTALL)
    TEXT_WIDGET_RE = re.compile(
        r'data-widget_type="text-editor\.default"[^>]*>(.*?)'
        r'(?=data-widget_type=|<div class="elementor-element|<section\b|\Z)', re.DOTALL)
    PRODUCT_SLUG_RE = re.compile(r"/product/([^/?#\"]+)")

    # ---- sizes ----
    _GRAM = r"(?:grams?|gms?|gr|g)"
    # The lookbehind refuses "/" so the "2 g" inside "1/2 g" is not read as 2 g.
    SIZE_RE = re.compile(r"(?<![\w./])(\d+(?:\.\d+)?|\.\d+)\s*" + _GRAM + r"(?!\w)", re.IGNORECASE)
    SIZE_WORD_PATTERNS = (
        (re.compile(r"(?<![\w./])1\s*/\s*2\s*" + _GRAM + r"(?!\w)"
                    r"|\bhalf(?:\s+a)?[\s-]*" + _GRAM + r"(?!\w)", re.IGNORECASE), 0.5),
        # "1/8" alone is as likely a date as a size, so it needs "th" or "oz".
        (re.compile(r"(?<![\w./])1\s*/\s*8\s*(?:th|oz|ounces?)(?!\w)|\beighths?\b", re.IGNORECASE), 3.5),
        (re.compile(r"(?<![\w./])1\s*/\s*4\s*(?:oz|ounces?)(?!\w)"
                    r"|\bquarter[\s-]*(?:oz|ounces?)(?!\w)", re.IGNORECASE), 7.0),
        (re.compile(r"(?<![\w./])1\s*/\s*2\s*(?:oz|ounces?)(?!\w)"
                    r"|\bhalf[\s-]*(?:oz|ounces?|zips?)(?!\w)", re.IGNORECASE), 14.0),
    )
    # What a size MEANS depends on the words around it. The menu writes the
    # lot size and the MOQ side by side ("20g available - 10g MOQ", "40g Lot -
    # 10g MOQ", "only 10g"), so reading every number as the amount you can buy
    # would be wrong in both directions.
    _SEP = r"\s*(?:[-–—:|]\s*)?"
    MOQ_AFTER_RE = re.compile(_SEP + r"(?:moq|minimum|min\b|min\.)", re.IGNORECASE)
    MOQ_BEFORE_RE = re.compile(
        r"\b(?:moq|minimum(?:\s+order)?(?:\s+quantity)?(?:\s+of)?|min\.?)\s*(?:is|of)?\s*[:=-]?\s*$",
        re.IGNORECASE)
    UNIT_AFTER_RE = re.compile(
        _SEP + r"(?:jars?|each|ea\b|units?|increments?|options?|sizes?|packs?|containers?"
        r"|portions?|samples?|retail|tins?|vials?)\b|\s*(?:[-–—:=@]|for)\s*\$",
        re.IGNORECASE)
    LOT_AFTER_RE = re.compile(
        _SEP + r"(?:lots?|available|avail\b|left|remaining|in\s+stock|total|on\s+hand|batch(?:es)?)\b",
        re.IGNORECASE)
    LOT_BEFORE_RE = re.compile(r"\b(?:only|total(?:\s+of)?|lot\s+of|batch\s+of)\s*$", re.IGNORECASE)
    UNIT_BEFORE_RE = re.compile(
        r"(?:\bin|\bper|/|\bof|\bsold\s+in|\bavailable\s+in|\bcomes\s+in|\boffered\s+in)\s*$",
        re.IGNORECASE)
    # "MOQ 1", "MOQ 100": a count of units, never grams.
    MOQ_COUNT_RE = re.compile(
        r"\b(?:moq|minimum\s+order(?:\s+quantity)?|min\.?\s+order)\s*(?:is|of)?\s*[:=-]?\s*"
        r"(\d+)(?![\d.]|\s*(?:grams?|gms?|gr|g|mg|kg|kilos?|lbs?|pounds?|oz|ounces?)(?!\w))",
        re.IGNORECASE)
    ANY_AMOUNT_RE = re.compile(
        r"\bno\s+(?:moq|minimums?)\b|\bby\s+the\s+gram\b|\bany\s+(?:amount|quantity|weight)\b"
        r"|\bsingle\s+grams?\b|\bgram\s+(?:jars?|increments?)\b", re.IGNORECASE)
    # Exactly one ounce. "QP (4 oz)", "8 oz", "1/2 oz" and "half zip" are not.
    OUNCE_RE = re.compile(
        r"(?<![\w./])(?:(\d+(?:\.\d+)?)\s*)?(?:oz|ozs|ounces?)(?!\w)"
        r"|\b(?:half[\s-]*)?zips?\b|\bzipper\b"
        r"|(?<![\w./])28\s*(?:grams?|gms?|gr|g)(?!\w)", re.IGNORECASE)

    # ---- what a line is ----
    # Matched against product names and menu titles only, never descriptions:
    # a rosin write-up may mention carts or prerolls without being one.
    EXCLUDE_RE = re.compile(
        r"\b(?:vapes?|vaporizers?|carts?|cartridges?|disposables?|pods?|pens?|510|aio"
        r"|all[\s-]in[\s-]one|batter(?:y|ies)|devices?|ikrusher"
        r"|pre[\s-]?rolls?|joints?|blunts?|hash\s*holes?"
        r"|infused|infusions?|edibles?|gumm(?:y|ies)|bon\s*bons?|honey\s*sticks?|capsules?"
        r"|tinctures?|syrups?|beverages?|drinks?|topicals?|lotions?|balms?|salves?)\b"
        r"|\d\s*mg\b", re.IGNORECASE)
    EXCLUDED_CATS = {"vape", "vapes", "edibles", "prerolls", "pre-rolls", "topicals"}
    CONCENTRATE_RE = re.compile(
        r"\b(?:rosin|hash|full\s*melt|temple\s*balls?|piatt?ell?a|dry\s*sift)\b", re.IGNORECASE)
    # "live-rosin" is the update cards' tag; "rosin" the store category, which
    # also covers listings whose names never say rosin ("Las Vegas Tier 1 -
    # Banana Punch").
    CONCENTRATE_CATS = {"rosin", "live-rosin"}
    # Not rosin or hash, and not flower either. Sugar and live resin are sold
    # by the ounce in 28 g options, so without this they would read as zips.
    OTHER_CONCENTRATE_RE = re.compile(
        r"\b(?:resins?|sugars?|diamonds?|sauce|badder|batter|budder|wax|shatter|crumble"
        r"|distillate|isolate|extracts?|concentrates?|kief|sift|shake|trim)\b", re.IGNORECASE)
    NON_FLOWER_CATS = {"rosin", "live-rosin", "extracts", "thca-sugar-resin",
                       "vape", "vapes", "edibles", "prerolls", "pre-rolls"}
    FLOWER_RE = re.compile(
        r"\b(?:flower|buds?|nugs?|indoor|mixed\s*light|greenhouse|light\s*deps?|deps"
        r"|smalls|bigs|popcorn|outdoor|sun\s*grown|zips?)\b", re.IGNORECASE)
    # On this site only rosin is priced per gram (flower is per lb/QP/oz,
    # sugar and resin per oz, vapes and gummies each), so an unlinked menu
    # line priced per gram is rosin even when its title never says so.
    PER_GRAM_RE = re.compile(r"\bper\s+(?:gram|g)\b|/\s*(?:gram|g)\b", re.IGNORECASE)
    # Likewise "Tier 1/2/3" is the rosin grade here (flower is "A Grade" /
    # "AAA"). It is the only signal on uncategorised rosin: "LV Lab Tier 1 -
    # Lemon Limez" has no category and never says rosin.
    TIER_RE = re.compile(r"\btier\s*[123]\b", re.IGNORECASE)
    NON_CONCENTRATE_CATS = {"flower", "thca-sugar-resin"}

    SOURCE_LABEL = {"menu": "drop menu", "card": "update card", "note": "production note"}

    def __init__(self, cfg):
        self.cfg = cfg
        self.menu_url = cfg.get("menu_url", self.MENU_URL)
        self.api_url = cfg.get("products_api_url", self.API_URL).rstrip("/")
        self.max_grams = float(cfg.get("max_concentrate_grams", 5))
        # The vendor's own grade label. It separates "AAA Indoor - Purple
        # Pixie Dust Ounces" ($70) from "AAA Indoor Exotic - Cherry Runtz
        # Ounces" ($80), which is the line the brief draws.
        words = cfg.get("exotic_keywords") or ["exotic"]
        self.exotic_re = re.compile(
            r"\b(?:%s)s?\b" % "|".join(re.escape(w) for w in words), re.IGNORECASE)

    # ---- small text helpers ----

    @staticmethod
    def _clean(fragment):
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment or "",
                      flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = htmllib.unescape(text).replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _words(text):
        return re.findall(r"[a-z]+", (text or "").lower())

    @staticmethod
    def _norm(text):
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    @classmethod
    def _image_words(cls, img_tag):
        src = re.search(r'src="([^"]+)"', img_tag)
        alt = re.search(r'alt="([^"]*)"', img_tag)
        stem = src.group(1).split("?")[0].rsplit("/", 1)[-1].rsplit(".", 1)[0] if src else ""
        return cls._words(stem + " " + (htmllib.unescape(alt.group(1)) if alt else ""))

    def _slug_of(self, href):
        if not href or href.startswith("#"):
            return None
        m = self.PRODUCT_SLUG_RE.search(href)
        return m.group(1).lower() if m else None

    @staticmethod
    def _price(prices):
        prices = prices or {}
        try:
            unit = 10 ** int(prices.get("currency_minor_unit", 2))
            rng = prices.get("price_range")
            if rng:
                return "$%.2f-$%.2f" % (int(rng["min_amount"]) / unit,
                                        int(rng["max_amount"]) / unit)
            return "$%.2f" % (int(prices["price"]) / unit)
        except (KeyError, TypeError, ValueError):
            return ""

    # ---- fetch ----

    def fetch(self, session, ua, timeout):
        resp = http_get(session, self.menu_url, ua, timeout,
                        expect_pattern=self.PRICE_LIST_RE)
        menu, cards, notes = self._parse_menu(resp.text)
        if not menu:
            # The page has been an Elementor price list since at least
            # 2026-10-13. Nothing parsed means the markup or the URL changed;
            # raise so the health counter sees it instead of a quiet page.
            raise RuntimeError("no price-list items parsed from %s; menu markup may "
                               "have changed" % self.menu_url)
        time.sleep(1.5)  # spacing for the rate limit; see _fetch_products
        products = self._fetch_products(session, ua, timeout)
        snapshot = {"products": products, "menu": menu, "cards": cards, "notes": notes}
        self._fetch_variation_stock(session, ua, timeout, snapshot)
        return snapshot

    def _fetch_products(self, session, ua, timeout):
        """Every product the store lists as buyable right now, minus the
        categories neither rule can ever match.

        Both filters run server-side to keep this to one request. The shop
        serves the API uncached (~3 s a page) from WordPress.com, whose edge
        answered 429 Too Many Requests on 2026-10-13 after a short burst of
        testing. Out of stock: an alert needs something to buy. Excluded
        categories (sugar/resin, vape, edibles, prerolls by ID in config):
        that took 286 products to 74, three pages to one. A category the
        config does not name is still fetched, so a new one costs load, never
        a missed listing.
        """
        products = []
        max_pages = int(self.cfg.get("max_pages", 10))
        exclude = ",".join(str(int(c)) for c in self.cfg.get("exclude_category_ids") or [])
        query = "per_page=100&stock_status=instock,onbackorder&_fields=" + self.API_FIELDS
        if exclude:
            query += "&category=%s&category_operator=not_in" % exclude
        page = 1
        while True:
            resp = http_get(session, "%s?%s&page=%d" % (self.api_url, query, page),
                            ua, timeout, expect_json=True)
            batch = resp.json()
            if not isinstance(batch, list):
                raise RuntimeError("store API did not return a product list")
            products.extend(self._compact_product(raw) for raw in batch
                            if raw.get("id") is not None)
            try:
                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
            except (TypeError, ValueError):
                total_pages = 1
            if page >= min(total_pages, max_pages):
                break
            page += 1
            time.sleep(1.5)
        if not products:
            # 74 matching products were in stock on 2026-10-13. Zero is a
            # broken feed, not an empty shop, and must not diff to "no change".
            raise RuntimeError("store API returned no in-stock products; "
                               "format may have changed")
        return products

    def _compact_product(self, raw):
        variations = []
        for v in raw.get("variations") or []:
            label = " ".join(
                self._clean(a.get("value")).strip(" \"'“”")
                for a in v.get("attributes") or []).strip()
            variations.append({"id": v.get("id"), "label": label})
        if variations:
            options = [v["label"] for v in variations]
        else:
            # Simple products still list their weights as attribute terms
            # ("Live Rosin - Weight: 10g, 28g, 56g, 100g"). That is the
            # vendor saying what sizes it sells, so it counts.
            options = ["%s: %s" % (self._clean(a.get("name")), self._clean(t.get("name")))
                       for a in raw.get("attributes") or [] for t in a.get("terms") or []]
        add_to_cart = raw.get("add_to_cart") or {}
        try:
            cart_min = int(add_to_cart.get("minimum") or 1)
        except (TypeError, ValueError):
            cart_min = 1
        return {
            "id": raw["id"],
            "slug": (raw.get("slug") or "").lower(),
            "name": self._clean(raw.get("name")),
            "url": raw.get("permalink") or self.menu_url,
            "cats": sorted({(c.get("slug") or "").lower()
                            for c in raw.get("categories") or []} - {""}),
            "tags": [self._clean(t.get("name") or t.get("slug"))
                     for t in raw.get("tags") or []],
            "in_stock": bool(raw.get("is_in_stock")) and bool(raw.get("is_purchasable", True)),
            "cart_min": cart_min,
            "options": options,
            "variations": variations,
            "short": self._clean(raw.get("short_description")),
            "desc": self._clean(raw.get("description")),
            "price": self._price(raw.get("prices")),
            "variation_stock": {},
        }

    def _fetch_variation_stock(self, session, ua, timeout, snapshot):
        """Per-variation stock for offers that qualify on SOME variations.

        A variable product reads in stock when ANY variation is, so an exotic
        strain sold as 28 g / QP / LB would read "in stock" while only the
        pound is (PROJECT.md: never aggregate availability across variants).
        Only offers that already qualify are looked up, so a normal run
        makes no extra requests at all.
        """
        cap = int(self.cfg.get("max_variation_checks_per_run", 10))
        checked = 0
        for offer in self._offers(snapshot).values():
            product = offer["product"]
            if product is None or not product["variations"]:
                continue
            verdict = self._evaluate(offer)
            vids = (verdict or {}).get("variation_ids") or []
            if not vids or set(vids) >= {v["id"] for v in product["variations"]}:
                continue
            for vid in vids:
                if checked >= cap:
                    logging.warning("[%s] variation stock lookups capped at %d this run",
                                    self.name, cap)
                    return
                checked += 1
                time.sleep(1.5)  # same rate-limit spacing as the catalog pages
                try:
                    resp = http_get(session, "%s/%s?_fields=id,is_in_stock,is_purchasable"
                                    % (self.api_url, vid), ua, timeout, expect_json=True)
                    data = resp.json()
                    product["variation_stock"][vid] = (
                        bool(data.get("is_in_stock")) and bool(data.get("is_purchasable", True)))
                except Exception as exc:
                    logging.warning("[%s] stock lookup for variation %s failed: %s",
                                    self.name, vid, exc)

    def _parse_menu(self, html_text):
        """Price-list lines, production-update cards and free-text notes from
        the page's own Elementor document (site header and footer excluded)."""
        start = max(html_text.find('data-elementor-type="wp-page"'), 0)
        end = html_text.find('data-elementor-type="footer"', start)
        doc = html_text[start:end if end > 0 else len(html_text)]

        # Lists are labelled by banner IMAGES ("Tier-1-rosin.png",
        # "VERITAS-FLOWER-MENU.png"), not text, so a banner's file name and
        # alt text are the only words a list has. A list with no banner of its
        # own belongs to the one above it.
        headers = [(m.start(), " ".join(self._image_words(m.group(1))))
                   for m in self.IMAGE_WIDGET_RE.finditer(doc)]
        headers += [(m.start(), " ".join(self._words(self._clean(m.group(1)))))
                    for m in self.HEADING_RE.finditer(doc)]
        headers.sort()

        def section_at(pos):
            label = ""
            for hpos, words in headers:
                if hpos > pos:
                    break
                label = words
            return label

        menu = []
        for m in self.LI_RE.finditer(doc):
            attrs, body = m.group(1), m.group(2)
            if ("elementor-price-list-item" not in attrs
                    and "elementor-price-list-item" not in body[:300]):
                continue
            title = self.TITLE_RE.search(body)
            if not title:
                continue
            price = self.PRICE_RE.search(body)
            desc = self.DESC_RE.search(body)
            href = self.HREF_RE.search(attrs + body[:300])
            menu.append({
                "source": "menu",
                "title": self._clean(title.group(1)),
                "texts": [self._clean(price.group(1)) if price else "",
                          self._clean(desc.group(1)) if desc else ""],
                "href": href.group(1) if href else None,
                "section": section_at(m.start()),
                "cats": [],
            })

        cards = []
        for m in self.CARD_RE.finditer(doc):
            block = m.group(0)
            title = re.search(r'vb-card__title"[^>]*>(.*?)</h\d>', block, re.DOTALL)
            if not title:
                continue
            note = re.search(r'vb-card__note"[^>]*>(.*?)</p>', block, re.DOTALL)
            price = re.search(r'vb-card__price"[^>]*>(.*?)</p>', block, re.DOTALL)
            badge = re.search(r'vb-badge[^"]*"[^>]*>(.*?)</span>', block, re.DOTALL)
            href = re.search(r'class="vb-btn"[^>]*href="([^"]+)"', block)
            cats = re.search(r'data-vb-cats="([^"]*)"', block)
            cards.append({
                "source": "card",
                "title": self._clean(title.group(1)),
                "texts": [self._clean(x.group(1)) if x else "" for x in (note, price, badge)],
                "href": href.group(1) if href else None,
                "section": "",
                "cats": (cats.group(1).lower().split() if cats else []),
            })

        notes = []
        for m in self.TEXT_WIDGET_RE.finditer(doc):
            for chunk in re.split(r"</li>|</p>|<br\s*/?>", m.group(1), flags=re.IGNORECASE):
                text = self._clean(chunk)
                if len(text) >= 4:
                    notes.append({"source": "note", "title": text[:400], "texts": [],
                                  "href": None, "section": "", "cats": []})
        return menu, cards, notes

    # ---- evaluation ----

    @classmethod
    def _size_context(cls, text, start, end):
        after = text[end:end + 30]
        before = text[max(0, start - 30):start]
        if cls.MOQ_AFTER_RE.match(after) or cls.MOQ_BEFORE_RE.search(before):
            return "moq"
        if cls.UNIT_AFTER_RE.match(after):
            return "unit"
        if cls.LOT_AFTER_RE.match(after) or cls.LOT_BEFORE_RE.search(before):
            return "lot"
        if cls.UNIT_BEFORE_RE.search(before):
            return "unit"
        return "bare"

    @classmethod
    def _size_mentions(cls, text):
        """(grams, context, snippet) for every size in `text`; context is
        "moq", "unit" (a size it is sold in), "lot" (how much exists) or
        "bare" (no telling)."""
        found = [(float(m.group(1)), m) for m in cls.SIZE_RE.finditer(text or "")]
        for rx, grams in cls.SIZE_WORD_PATTERNS:
            found.extend((grams, m) for m in rx.finditer(text or ""))
        return [(grams, cls._size_context(text, m.start(), m.end()), m.group(0).strip())
                for grams, m in found]

    @classmethod
    def _ounce_mentions(cls, text):
        hits = []
        for m in cls.OUNCE_RE.finditer(text or ""):
            token = m.group(0).strip()
            before = text[max(0, m.start() - 12):m.start()]
            if token.lower().startswith("half"):
                continue
            if re.search(r"(?:half|quarter|eighth)[\s-]*$|\d\s*/\s*\d+\s*(?:th)?\s*$",
                         before, re.IGNORECASE):
                continue
            count = m.group(1)
            if count is None and not token.startswith("28"):
                lead = re.search(r"(\d+(?:\.\d+)?)\s*$", before)
                count = lead.group(1) if lead else None
            if count is not None and float(count) != 1.0:
                continue
            hits.append(token)
        return hits

    def _min_purchase(self, source="store", ident="", options=(), texts=(), cart_min=None,
                      ident_label="name", bare_ident=True):
        """Smallest amount, in grams, that one source says can be bought.

        Returns (grams, evidence), or (None, None) when the source says
        nothing usable. A stated gram MOQ wins; a unit size ("2 Gram Jar", a
        "2g" option) is multiplied by any count minimum (an "MOQ 10" of 1 g
        jars is 10 g); a cart minimum of 2-5 counts because someone set it on
        purpose, and the default of 1 does not.
        """
        units, moq_g, moq_n, any_amount = [], [], [], []
        blocks = ([(ident, "%s %s" % (source, ident_label), bare_ident)]
                  + [(o, "%s option" % source, True) for o in options]
                  + [(t, "%s text" % source, False) for t in texts])
        for text, where, bare_counts in blocks:
            if not text:
                continue
            for grams, ctx, snippet in self._size_mentions(text):
                evidence = '%s "%s"' % (where, snippet)
                if ctx == "moq":
                    moq_g.append((grams, evidence + " MOQ"))
                elif ctx == "unit" or (ctx == "bare" and bare_counts):
                    units.append((grams, evidence))
            for m in self.MOQ_COUNT_RE.finditer(text):
                moq_n.append((int(m.group(1)), '%s "%s"' % (where, m.group(0).strip())))
            m = self.ANY_AMOUNT_RE.search(text)
            if m:
                any_amount.append('%s "%s"' % (where, m.group(0).strip()))
        cart = cart_min if cart_min and cart_min > 1 else None
        cart_why = "cart minimum %d" % cart if cart else None
        if moq_g:
            grams, why = min(moq_g)
            if cart and not units and cart > grams:
                return float(cart), cart_why
            return grams, why
        if units:
            size, why = min(units)
            count = max([1] + [n for n, _ in moq_n] + ([cart] if cart else []))
            if count > 1:
                return size * count, "%s x minimum %d" % (why, count)
            return size, why
        if moq_n:
            n, why = min(moq_n)
            if cart and cart > n:
                return float(cart), cart_why
            return float(n), why
        if any_amount:
            return (float(cart), cart_why) if cart else (1.0, any_amount[0])
        if cart:
            return float(cart), cart_why
        return None, None

    def _offers(self, snapshot):
        """Group every line into offers keyed by the thing you would buy.

        A menu line or update card that links to a store product joins that
        product's offer, so one listing never alerts twice. A link to a
        product the store does not list as in stock (sold out, or deleted)
        means the line cannot be bought right now and is dropped.
        """
        products = snapshot.get("products", [])
        by_slug = {p["slug"]: p for p in products if p.get("slug")}
        offers = {"p:%s" % p["id"]: {"product": p, "items": [], "title": p["name"],
                                      "url": p["url"], "price": p["price"]}
                  for p in products}
        lines = snapshot.get("menu", []) + snapshot.get("cards", []) + snapshot.get("notes", [])
        for item in lines:
            slug = self._slug_of(item.get("href"))
            if slug:
                product = by_slug.get(slug)
                if product is not None:
                    offers["p:%s" % product["id"]]["items"].append(item)
                continue
            key = ("n:" if item["source"] == "note" else "m:") + self._norm(item["title"])[:100]
            # Menu texts are [price, description]; card texts [note, price, badge].
            price = {"menu": 0, "card": 1}.get(item["source"])
            offer = offers.setdefault(key, {
                "product": None, "items": [], "title": item["title"], "url": self.menu_url,
                "price": item["texts"][price] if price is not None else "",
            })
            offer["items"].append(item)
        return offers

    def _evaluate(self, offer):
        """{"kind", "why", "variation_ids"} if the offer meets a rule, else None.

        Stock is judged separately (_available), so a listing that qualifies
        while sold out alerts the moment it is back.
        """
        product = offer["product"]
        items = offer["items"]
        ident = " | ".join(([product["name"]] if product else []) + [i["title"] for i in items])
        section = " ".join(i["section"] for i in items if i.get("section"))
        if product is not None:
            # The store's categories, not the update cards' tags: a rosin card
            # on 2026-10-13 was tagged "flower".
            cats = set(product["cats"])
        else:
            cats = {c for i in items for c in i.get("cats", [])}
        if self.EXCLUDE_RE.search(ident) or cats & self.EXCLUDED_CATS:
            return None
        if self._is_concentrate(product, items, ident, section, cats):
            return self._rosin_verdict(product, items)
        if self._is_flower(product, ident, section, cats):
            return self._ounce_verdict(product, items, ident, section)
        return None

    def _is_concentrate(self, product, items, ident, section, cats):
        """Rosin or hash. Live resin and sugar are neither."""
        if (self.CONCENTRATE_RE.search(ident) or self.CONCENTRATE_RE.search(section)
                or cats & self.CONCENTRATE_CATS):
            return True
        if cats & self.NON_CONCENTRATE_CATS:
            return False
        if self.OTHER_CONCENTRATE_RE.search(ident + " " + section) or self.FLOWER_RE.search(ident):
            return False
        return bool(self.TIER_RE.search(ident)
                    or (product is None
                        and any(self.PER_GRAM_RE.search(t) for i in items for t in i["texts"])))

    def _is_flower(self, product, ident, section, cats):
        if product is not None and product["cats"]:
            return ("flower" in cats
                    or (not cats & self.NON_FLOWER_CATS
                        and bool(self.FLOWER_RE.search(product["name"]))))
        if self.OTHER_CONCENTRATE_RE.search(ident + " " + section):
            return False
        return bool("flower" in cats or self.FLOWER_RE.search(ident)
                    or self.FLOWER_RE.search(section))

    def _rosin_verdict(self, product, items):
        cart = product["cart_min"] if product else None
        sources = []
        if product is not None:
            sources.append(({"ident": product["name"],
                             "texts": [product["short"], product["desc"]]}, None))
            if product["options"]:
                vids = [v["id"] for v in product["variations"]
                        if any(g <= self.max_grams for g, _, _ in self._size_mentions(v["label"]))]
                sources.append(({"options": product["options"]},
                                vids if product["variations"] else None))
        for item in items:
            note = item["source"] == "note"
            # A note is prose: "we pressed 5g" is not a 5 g unit for sale.
            sources.append(({"source": self.SOURCE_LABEL[item["source"]],
                             "ident": item["title"], "texts": item["texts"],
                             "ident_label": "text" if note else "title",
                             "bare_ident": not note}, None))
        for kwargs, vids in sources:
            grams, why = self._min_purchase(cart_min=cart, **kwargs)
            if grams is not None and grams <= self.max_grams:
                return {"kind": "ROSIN_5G", "why": "buyable in %g g: %s" % (grams, why),
                        "variation_ids": vids}
        return None

    def _ounce_verdict(self, product, items, ident, section):
        # Exotic only from names, titles, tags and the vendor's own menu and
        # card wording -- never from store descriptions, which describe the
        # strain ("an exotic cross of...") rather than grade it.
        exotic_texts = [ident, section] + [t for i in items for t in i["texts"]]
        if product is not None:
            exotic_texts += product["tags"] + product["cats"]
        if not any(self.exotic_re.search(t) for t in exotic_texts if t):
            return None
        whole = [("%s %s" % (self.SOURCE_LABEL[i["source"]], "text" if n else "title"), t)
                 for i in items for n, t in enumerate([i["title"]] + i["texts"])]
        if product is not None:
            whole = [("store name", product["name"]),
                     ("store description", product["short"])] + whole
        whole.append(("menu section", section))
        for where, text in whole:
            hits = self._ounce_mentions(text)
            if hits:
                return {"kind": "EXOTIC_OZ", "why": 'ounce: %s "%s"' % (where, hits[0]),
                        "variation_ids": None}
        if product is not None and product["variations"]:
            matching = [v for v in product["variations"] if self._ounce_mentions(v["label"])]
            if matching:
                return {"kind": "EXOTIC_OZ",
                        "why": 'ounce: store option "%s"' % matching[0]["label"],
                        "variation_ids": [v["id"] for v in matching]}
        elif product is not None:
            matching = [o for o in product["options"] if self._ounce_mentions(o)]
            if matching:
                return {"kind": "EXOTIC_OZ", "why": 'ounce: store option "%s"' % matching[0],
                        "variation_ids": None}
        return None

    @staticmethod
    def _available(offer, verdict):
        product = offer["product"]
        if product is None:
            return True  # a menu line or note with no store product: listed = offered
        if not product["in_stock"]:
            return False
        vids = verdict.get("variation_ids")
        if not vids or set(vids) >= {v["id"] for v in product["variations"]}:
            return True
        known = [product["variation_stock"][v] for v in vids if v in product["variation_stock"]]
        # Lookup failed: fall back to the product's own flag rather than go
        # blind on the one listing that matters.
        return any(known) if known else True

    def diff(self, prev_state, current):
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        prev_available = prev_state.get("available") or {}
        alerted_at = dict(prev_state.get("alerted_at") or {})
        seen = dict(prev_state.get("seen") or {})
        # A listing that drops out and comes straight back (an order placed
        # and cancelled, a stock correction, the menu being edited) is one
        # event, not two.
        cooldown = timedelta(hours=float(self.cfg.get("realert_after_hours", 12)))

        qualifying = {}
        for key, offer in sorted(self._offers(current).items()):
            verdict = self._evaluate(offer)
            if verdict is not None and self._available(offer, verdict):
                qualifying[key] = (offer, verdict)
        if qualifying:
            logging.info("[%s] on offer and qualifying: %s", self.name,
                         "; ".join("%s %s" % (v["kind"], o["title"])
                                   for o, v in qualifying.values()))

        alerts = []
        for key, (offer, verdict) in sorted(qualifying.items()):
            if key in prev_available:
                continue  # on offer since the last run: already told
            last = alerted_at.get(key)
            if last:
                try:
                    if now - datetime.fromisoformat(last) < cooldown:
                        logging.info("[%s] %s re-qualified within %s of its last alert; "
                                     "not re-sent", self.name, key, cooldown)
                        continue
                except ValueError:
                    pass
            if offer["product"] is not None:
                status = "back in stock" if key in seen else "new"
            elif key.startswith("n:"):
                status = "production note on the drop menu"
            else:
                status = "on the drop menu, no store listing"
            alerts.append({
                "site": self.name, "label": self.label,
                "kind": verdict["kind"], "title": offer["title"][:200],
                "url": offer["url"],
                "details": "; ".join(x for x in (verdict["why"], offer["price"], status) if x),
            })
            alerted_at[key] = now_iso

        for key in qualifying:
            seen.setdefault(key, now_iso)
        horizon = now - timedelta(days=60)

        def recent(iso):
            try:
                return datetime.fromisoformat(iso) >= horizon
            except (TypeError, ValueError):
                return False

        new_state = {
            "v": 1,
            "available": {key: {"kind": verdict["kind"], "title": offer["title"][:160]}
                          for key, (offer, verdict) in qualifying.items()},
            "alerted_at": {k: v for k, v in alerted_at.items() if recent(v)},
            "seen": seen,
            # How much each source parsed. A count that collapses is the first
            # sign of a redesign that has not broken parsing outright.
            "counts": {k: len(current.get(k, [])) for k in ("products", "menu", "cards", "notes")},
            "last_seen": now_iso,
        }
        return new_state, alerts


SITE_CLASSES = {
    "typ3": Typ3Site,
    "hempbarn_livingsoil": HempBarnLivingSoilSite,
    "hempbarn_organicsoil": HempBarnOrganicSoilSite,
    "caregiverpharms": CaregiverPharmsSite,
    "flowgardens_smalls": FlowGardensSmallsSite,
    "fiveleafwellness": FiveLeafWellnessSite,
    "beleafer_indoor": BeleaferIndoorSite,
    "highalpinegenetics": HighAlpineGeneticsSite,
    "antheiafarms": AntheiaFarmsSite,
    "veritas": VeritasSite,
}


def build_sites(cfg):
    sites = []
    for key, klass in SITE_CLASSES.items():
        site_cfg = cfg.get(key, {})
        if not site_cfg.get("enabled", True):
            continue
        sites.append(klass(site_cfg))
    return sites


# ---------- Email -------------------------------------------------------

def _row(a):
    kind_color = {
        "DROP": "#c0392b",
        "RESTOCK": "#27ae60",
        "NEW_PRODUCT": "#c0392b",
        "NEW_STRAIN": "#c0392b",
        "SMALLS_BACK": "#2980b9",
        # A size this shop has never offered before. Rare by design, so give
        # it its own colour rather than letting it read as a routine restock.
        "NEW_VARIANT": "#8e44ad",
        # VERITAS: rosin/hash buyable in 5 g or less; an exotic ounce.
        "ROSIN_5G": "#d35400",
        "EXOTIC_OZ": "#16a085",
    }.get(a["kind"], "#333")
    return (
        '<tr>'
        f'<td style="color:{kind_color};font-weight:bold;">{htmllib.escape(a["kind"])}</td>'
        f'<td>{htmllib.escape(a["title"])}</td>'
        f'<td>{htmllib.escape(a.get("details") or "")}</td>'
        f'<td><a href="{htmllib.escape(a["url"])}">link</a></td>'
        '</tr>'
    )


def build_email_bodies(alerts_by_site, total_alerts):
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sections_html = []
    sections_text = []
    for site_label, alerts in alerts_by_site.items():
        rows = "".join(_row(a) for a in alerts)
        sections_html.append(
            f'<h3 style="margin-top:24px;">{htmllib.escape(site_label)} '
            f'<span style="color:#888;font-weight:normal;">({len(alerts)})</span></h3>'
            '<table border="1" cellpadding="6" cellspacing="0" '
            'style="border-collapse:collapse;font-size:14px;">'
            '<tr><th>Kind</th><th>Item</th><th>Details</th><th>Link</th></tr>'
            f'{rows}</table>'
        )
        sections_text.append(f"\n=== {site_label} ({len(alerts)}) ===")
        for a in alerts:
            sections_text.append(
                f"  {a['kind']:<12} {a['title']} | {a.get('details','')} | {a['url']}"
            )

    html = (
        '<html><body style="font-family:Arial,sans-serif;">'
        f'<h2>Drop Monitor — {when}</h2>'
        f'<p>{total_alerts} alert(s) across {len(alerts_by_site)} site(s).</p>'
        + "".join(sections_html)
        + '<p style="color:#888;font-size:11px;margin-top:24px;">'
        'Sent by drop-monitor (multi-site).'
        '</p></body></html>'
    )
    text = f"Drop Monitor — {when}\n{total_alerts} alert(s).\n" + "\n".join(sections_text)
    return html, text


def send_email(cfg, secrets, subject, html_body, text_body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = secrets["smtp_user"]
    msg["To"] = cfg["email_to"]
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    host = secrets["smtp_host"]
    port = int(secrets["smtp_port"])
    user = secrets["smtp_user"]
    pw = secrets["smtp_password"]
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.send_message(msg)


def email_alerts(all_alerts, cfg):
    if not cfg.get("email_enabled", False) or not all_alerts:
        return True  # email intentionally off / nothing to deliver
    if not cfg.get("email_to"):
        logging.error("email_to not set (no EMAIL_TO env var or secrets entry); skipping email")
        return False
    secrets = load_secrets()
    if not secrets:
        logging.error("no SMTP secrets available; cannot deliver %d alert(s)",
                      len(all_alerts))
        return False
    by_site = {}
    for a in all_alerts:
        by_site.setdefault(a["label"], []).append(a)
    counts_by_site = ", ".join(f"{lbl}: {len(v)}" for lbl, v in by_site.items())
    prefix = cfg.get("email_subject_prefix", "[Drop Monitor]")
    subject = f"{prefix} {len(all_alerts)} alert(s) — {counts_by_site}"
    html_body, text_body = build_email_bodies(by_site, len(all_alerts))
    # Retry transient SMTP failures (network blip, brief Gmail rate-limit) so a
    # one-off hiccup does not drop a real alert. main() saves state right after
    # this returns -- which suppresses re-alerting -- so the deliver-or-give-up
    # decision has to be made here, not deferred to the next run.
    attempts = max(1, int(cfg.get("email_retry_attempts", 3)))
    for attempt in range(1, attempts + 1):
        try:
            send_email(cfg, secrets, subject, html_body, text_body)
            logging.warning("email sent to %s: %s", cfg["email_to"], subject)
            return True
        except Exception as exc:
            logging.error("email send attempt %d/%d failed: %s",
                          attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 20))
    logging.error("email send FAILED after %d attempts; %d alert(s) NOT "
                  "delivered: %s", attempts, len(all_alerts), subject)
    return False


# ---------- Windows-only desktop alerts ---------------------------------

def fire_toast(title, body, url):
    if not (IS_WINDOWS and HAS_TOAST):
        return
    try:
        toast = Notification(
            app_id="Drop Monitor", title=title, msg=body, duration="long",
        )
        toast.set_audio(audio.LoopingAlarm, loop=False)
        toast.add_actions(label="Open", launch=url)
        toast.show()
    except Exception as exc:
        logging.warning("toast failed: %s", exc)


def play_alert_sound():
    if not IS_WINDOWS or winsound is None:
        return
    try:
        for _ in range(3):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            time.sleep(0.3)
    except Exception as exc:
        logging.warning("sound failed: %s", exc)


def desktop_alerts(alerts, cfg):
    if not IS_WINDOWS or not alerts:
        return
    if cfg.get("play_sound", True):
        play_alert_sound()
    if cfg.get("show_toast", True):
        for a in alerts:
            fire_toast(
                f"{a['label']} — {a['kind']}",
                f"{a['title']} ({a.get('details','')})",
                a["url"],
            )
    if cfg.get("open_browser_on_alert", True):
        cap = int(cfg.get("max_browser_tabs_per_run", 5))
        for a in alerts[:cap]:
            try:
                webbrowser.open(a["url"], new=2)
            except Exception as exc:
                logging.warning("browser open failed for %s: %s", a["url"], exc)


# ---------- Main loop ---------------------------------------------------

def run_test_email():
    setup_logging()
    cfg = load_config()
    logging.info("=== test email ===")
    if not cfg.get("email_to"):
        logging.error("email_to not set (no EMAIL_TO env var or secrets entry)")
        return 2
    secrets = load_secrets()
    if not secrets:
        logging.error("cannot send test: no secrets")
        return 2
    sample_alerts = [
        {"site": "typ3", "label": "TYP3 Cannabis", "kind": "DROP",
         "title": "Example Flower Drop (TEST)",
         "url": "https://typ3cannabis.com/products/example",
         "details": "$30.00"},
        {"site": "caregiverpharms", "label": "Caregiver Pharms",
         "kind": "SMALLS_BACK", "title": "Example THCa Flower (TEST)",
         "url": "https://caregiverpharms.com/products/example",
         "details": "smalls/micros variant available"},
        {"site": "hempbarn_livingsoil", "label": "Hemp Barn — Living Soil",
         "kind": "NEW_STRAIN", "title": "Example Strain (TEST)",
         "url": "https://www.thehempbarn.com/product/livingsoil/",
         "details": "added to strain dropdown"},
    ]
    by_site = {}
    for a in sample_alerts:
        by_site.setdefault(a["label"], []).append(a)
    html_body, text_body = build_email_bodies(by_site, len(sample_alerts))
    subject = f"{cfg.get('email_subject_prefix', '[Drop Monitor]')} test email"
    try:
        send_email(cfg, secrets, subject, html_body, text_body)
        logging.info("test email sent to %s", cfg["email_to"])
        return 0
    except Exception as exc:
        logging.error("test email failed: %s", exc)
        return 1


def main():
    setup_logging()
    cfg = load_config()
    logging.info("=== run start ===")

    sites = build_sites(cfg)
    state = load_state()
    state.setdefault("sites", {})

    session = requests.Session()
    ua = cfg.get("user_agent", DEFAULT_USER_AGENT)
    timeout = cfg.get("timeout_sec", 25)

    all_alerts = []
    summary_lines = []
    failure_threshold = max(1, int(cfg.get("site_failure_threshold", 3)))
    degraded_after_minutes = float(cfg.get("site_degraded_after_minutes", 120))

    now_utc = datetime.now(timezone.utc)
    for site in sites:
        prev = state["sites"].get(site.name, {})
        first_run_for_site = not prev

        # Per-site throttle: skip if min_interval_minutes has not elapsed.
        min_interval = float(site.cfg.get("min_interval_minutes", 0) or 0)
        if min_interval > 0 and prev.get("last_polled_at"):
            try:
                last_dt = datetime.fromisoformat(prev["last_polled_at"])
                elapsed_min = (now_utc - last_dt).total_seconds() / 60.0
                if elapsed_min < min_interval:
                    summary_lines.append(
                        f"{site.name}: skipped ({elapsed_min:.1f}m / {min_interval:.0f}m)"
                    )
                    continue
            except Exception as exc:
                logging.warning("[%s] could not parse last_polled_at: %s",
                                site.name, exc)

        try:
            current = site.fetch(session, ua, timeout)
        except Exception as exc:
            logging.error("[%s] fetch failed: %s", site.name, exc)
            count = record_site_failure(state, site.name, exc, failure_threshold)
            summary_lines.append(
                f"{site.name}: FETCH FAILED ({count}/{failure_threshold}: {exc})"
            )
            continue

        try:
            new_state, alerts = site.diff(prev, current)
        except Exception as exc:
            logging.error("[%s] diff failed: %s", site.name, exc)
            count = record_site_failure(state, site.name, exc, failure_threshold)
            summary_lines.append(
                f"{site.name}: DIFF FAILED ({count}/{failure_threshold}: {exc})"
            )
            continue

        clear_site_failure(state, site.name)

        # Record the poll time so the next run can apply throttling.
        new_state["last_polled_at"] = now_utc.isoformat()

        if first_run_for_site:
            logging.info("[%s] first run: seeding (no alerts fired)", site.name)
            state["sites"][site.name] = new_state
            summary_lines.append(f"{site.name}: seeded")
            continue

        if alerts:
            logging.warning("[%s] %d alert(s)", site.name, len(alerts))
            for a in alerts:
                logging.warning("  %s | %s | %s | %s",
                                a["kind"], a["title"], a.get("details", ""), a["url"])
        state["sites"][site.name] = new_state
        all_alerts.extend(alerts)
        summary_lines.append(f"{site.name}: {len(alerts)} alert(s)")

    email_ok = True
    if all_alerts:
        desktop_alerts(all_alerts, cfg)
        email_ok = email_alerts(all_alerts, cfg)

    persistent_failures = persistent_site_failures(
        state, failure_threshold, {site.name for site in sites}
    )
    degraded = degraded_site_failures(persistent_failures, degraded_after_minutes)

    # The Healthchecks check answers exactly ONE question: is the monitor
    # running and able to tell me about a drop? A single flaky STORE is not
    # that question. Letting any failing site flip this produced a DOWN/UP
    # pair every time Beleafer's Cloudflare block came and went -- 4+ pairs a
    # day of pure noise, which trains you to ignore the one email that
    # matters. Site trouble is recorded in state for the triage task to pick
    # up; only undelivered alerts (or no ping at all, which is the
    # dead-man's-switch doing its real job) put the check down.
    unhealthy = not email_ok
    health = state.setdefault("health", {})
    health["unhealthy"] = unhealthy
    health["degraded_sites"] = sorted(degraded)
    health["checked_at"] = datetime.now(timezone.utc).isoformat()

    if persistent_failures:
        summary_lines.append("SITES FAILING: " + ", ".join(
            f"{name} ({entry['count']} consecutive: {entry['last_error']})"
            for name, entry in sorted(persistent_failures.items())))
    if degraded:
        summary_lines.append("DEGRADED (>%dm): %s"
                             % (degraded_after_minutes, ", ".join(sorted(degraded))))

    health_body = None
    if unhealthy:
        parts = ["alert email delivery FAILED after retries"]
        if persistent_failures:
            parts.append("also failing: " + ", ".join(sorted(persistent_failures)))
        health_body = "\n".join(parts)

    save_state(state)
    logging.info("=== run end: %d total alert(s); %s ===",
                 len(all_alerts), "; ".join(summary_lines))
    health_ok = ping_healthcheck(success=not unhealthy, body=health_body)
    # Exit non-zero ONLY for undelivered alerts. A failing site must not fail
    # the job: the loop reads this exit code, and a wedged store would
    # otherwise mark every 5h30m run as failed and generate GitHub failure
    # mail on top of everything else.
    return 0 if not unhealthy and health_ok is not False else 1


def ping_healthcheck(success=True, body=None):
    # Strip whitespace and any stray BOM that can sneak in via secret managers.
    url = os.environ.get("HEALTHCHECK_URL", "").strip().lstrip("﻿").strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        logging.warning("healthcheck URL is invalid")
        return False
    # On delivery failure, ping the "/fail" endpoint so healthchecks.io flags
    # the check down and notifies the user from ITS OWN infrastructure -- an
    # alert path independent of the SMTP that just failed. A later healthy run
    # pings the base URL and clears it.
    target = url if success else url.rstrip("/") + "/fail"
    try:
        if body:
            # Healthchecks stores the ping body and shows it on the check and in
            # its notifications. Without this the DOWN email says only that the
            # check is down, so a broken SITE and a dead RUNNER look identical
            # and neither names what failed. Capped well under the 100KB limit.
            response = requests.post(
                target, data=body[:8000].encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"}, timeout=10)
        else:
            response = requests.get(target, timeout=10)
        response.raise_for_status()
        return True
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        logging.warning("healthcheck ping failed (%s)", detail)
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-site drop monitor.")
    parser.add_argument(
        "--test-email", action="store_true",
        help="Send a sample alert email and exit (does not touch state).",
    )
    args = parser.parse_args()
    sys.exit(run_test_email() if args.test_email else main())
