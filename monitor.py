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
    },
    "highalpinegenetics": {
        "enabled": True,
        "search_url": "https://www.highalpinegenetics.com/apps/search?q=+",
        "allowed_types": [1, 2],
        "max_pages": 30,
        "min_interval_minutes": 5,
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

def http_get(session, url, ua, timeout, expect_json=False):
    headers = {
        "User-Agent": ua,
        "Accept": "application/json" if expect_json else
                  "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = session.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"
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

    @classmethod
    def _extract_descriptions(cls, html_text, strains):
        m = cls.SHORT_DESC_RE.search(html_text)
        if not m:
            return {}
        block = m.group(1)
        positions = []
        for strain in strains:
            pat = re.compile(
                r'<strong>\s*' + re.escape(strain) + r'\b[^<]*</strong>',
                re.IGNORECASE,
            )
            for sm in pat.finditer(block):
                positions.append((sm.start(), sm.end(), strain))
        positions.sort()
        descs = {}
        for i, (start, end, strain) in enumerate(positions):
            nxt = positions[i + 1][0] if i + 1 < len(positions) else len(block)
            chunk = block[end:nxt]
            text = re.sub(r"<[^>]+>", " ", chunk)
            text = htmllib.unescape(text)
            text = text.replace("\xa0", " ")
            text = re.sub(r"\s+", " ", text).strip()
            if strain not in descs or len(text) > len(descs[strain]):
                descs[strain] = text
        return descs

    def fetch(self, session, ua, timeout):
        resp = http_get(session, self.cfg["url"], ua, timeout)
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
        # Merge descriptions so a strain keeps its best-known description even
        # if one run fails to re-extract it. Preserve all previously-seen
        # strains so one falling off the dropdown does not re-alert later.
        merged_descriptions = dict(prev_state.get("descriptions", {}))
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
            for v in variants:
                if self._is_smalls_variant(v.get("title")):
                    smalls_variant_present = True
                    if v.get("available", False):
                        smalls_variant_available = True
            out[p["handle"]] = {
                "title": p.get("title", p["handle"]),
                "any_available": any_available,
                "smalls_present": smalls_variant_present,
                "smalls_available": smalls_variant_available,
            }
        return out

    def diff(self, prev_state, current):
        now_iso = datetime.now(timezone.utc).isoformat()
        prev = prev_state.get("products", {})
        alerts = []
        product_url_base = "https://caregiverpharms.com/products/"
        for handle, p in current.items():
            url = product_url_base + handle
            is_new = handle not in prev
            if is_new:
                kind = "NEW_PRODUCT"
                detail = "in stock" if p["any_available"] else "sold out"
                if p["smalls_available"]:
                    detail += " — smalls/micros AVAILABLE"
                alerts.append({
                    "site": self.name, "label": self.label,
                    "kind": kind, "title": p["title"],
                    "url": url, "details": detail,
                })
                continue
            was = prev[handle]
            if p["any_available"] and not was.get("any_available", False):
                alerts.append({
                    "site": self.name, "label": self.label,
                    "kind": "RESTOCK", "title": p["title"],
                    "url": url, "details": "back in stock",
                })
            if p["smalls_available"] and not was.get("smalls_available", False):
                alerts.append({
                    "site": self.name, "label": self.label,
                    "kind": "SMALLS_BACK", "title": p["title"],
                    "url": url,
                    "details": "smalls/micros variant available",
                })
        # Preserve previously-seen handles to prevent re-alerts when a
        # product temporarily disappears from the collection JSON.
        merged_products = dict(prev)
        for handle, p in current.items():
            merged_products[handle] = dict(p)
        new_state = {"products": merged_products, "last_seen": now_iso}
        return new_state, alerts


class FlowGardensSmallsSite:
    name = "flowgardens_smalls"
    label = "Flow Gardens — Smalls"

    # Strain naming convention is "<Strain Name> - Type N (...)".
    # \b prevents Type 2 from matching "Type 20" if the merchant ever uses double digits.
    TYPE_RE = re.compile(r"\btype\s*(\d+)\b", re.IGNORECASE)

    def __init__(self, cfg):
        self.cfg = cfg
        at = cfg.get("allowed_types")
        self.allowed_types = set(int(x) for x in at) if at else None

    @classmethod
    def _strain_type(cls, strain):
        m = cls.TYPE_RE.search(strain)
        return int(m.group(1)) if m else None

    def fetch(self, session, ua, timeout):
        resp = http_get(session, self.cfg["product_json_url"], ua, timeout, expect_json=True)
        data = resp.json()
        product = data.get("product") or {}
        strains = set()
        for v in product.get("variants", []) or []:
            opt1 = v.get("option1")
            if opt1:
                strains.add(opt1.strip())
        for opt in product.get("options", []) or []:
            if opt.get("name", "").lower() == "strain":
                for value in opt.get("values", []) or []:
                    strains.add(value.strip())
        return {"strains": sorted(strains)}

    def diff(self, prev_state, current):
        now_iso = datetime.now(timezone.utc).isoformat()
        prev_strains = set(prev_state.get("strains", []))
        curr_strains = set(current.get("strains", []))
        is_first_run = not prev_strains
        new_ones = sorted(curr_strains - prev_strains)
        alerts = []
        for s in new_ones:
            t = self._strain_type(s)
            if self.allowed_types is not None and t not in self.allowed_types:
                if not is_first_run:
                    logging.info(
                        "[%s] new strain '%s' suppressed: type=%s not in %s",
                        self.name, s, t, sorted(self.allowed_types),
                    )
                continue
            details = f"Type {t}" if t is not None else "added to strain dropdown"
            alerts.append({
                "site": self.name, "label": self.label,
                "kind": "NEW_STRAIN", "title": s,
                "url": self.cfg["product_url"],
                "details": details,
            })
        # Preserve all previously-seen strains so that one falling off the
        # dropdown does not re-alert later when it returns.
        new_state = {
            "strains": sorted(prev_strains | curr_strains),
            "last_seen": now_iso,
        }
        return new_state, alerts


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
        self._session = None
        self._ua = None
        self._timeout = None

    def _slug_of(self, url):
        m = self.SLUG_RE.match(url)
        return m.group(1) if m else url

    def _fetch_product_page(self, url):
        try:
            resp = http_get(self._session, url, self._ua, self._timeout)
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
                resp = http_get(session, page_url, ua, timeout)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    break
                raise
            page_urls = set(self.PRODUCT_URL_RE.findall(resp.text))
            new = page_urls - urls
            if not new:
                break
            urls |= new
            time.sleep(0.4)
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
                resp = http_get(session, page_url, ua, timeout)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
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


SITE_CLASSES = {
    "typ3": Typ3Site,
    "hempbarn_livingsoil": HempBarnLivingSoilSite,
    "hempbarn_organicsoil": HempBarnOrganicSoilSite,
    "caregiverpharms": CaregiverPharmsSite,
    "flowgardens_smalls": FlowGardensSmallsSite,
    "fiveleafwellness": FiveLeafWellnessSite,
    "beleafer_indoor": BeleaferIndoorSite,
    "highalpinegenetics": HighAlpineGeneticsSite,
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
        return
    if not cfg.get("email_to"):
        logging.error("email_to not set (no EMAIL_TO env var or secrets entry); skipping email")
        return
    secrets = load_secrets()
    if not secrets:
        return
    by_site = {}
    for a in all_alerts:
        by_site.setdefault(a["label"], []).append(a)
    counts_by_site = ", ".join(f"{lbl}: {len(v)}" for lbl, v in by_site.items())
    prefix = cfg.get("email_subject_prefix", "[Drop Monitor]")
    subject = f"{prefix} {len(all_alerts)} alert(s) — {counts_by_site}"
    html_body, text_body = build_email_bodies(by_site, len(all_alerts))
    try:
        send_email(cfg, secrets, subject, html_body, text_body)
        logging.warning("email sent to %s: %s", cfg["email_to"], subject)
    except Exception as exc:
        logging.error("email send failed: %s", exc)


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
    any_fetch_failed = False

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
            summary_lines.append(f"{site.name}: FETCH FAILED ({exc})")
            any_fetch_failed = True
            continue

        try:
            new_state, alerts = site.diff(prev, current)
        except Exception as exc:
            logging.error("[%s] diff failed: %s", site.name, exc)
            summary_lines.append(f"{site.name}: DIFF FAILED ({exc})")
            continue

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

    if all_alerts:
        desktop_alerts(all_alerts, cfg)
        email_alerts(all_alerts, cfg)

    save_state(state)
    logging.info("=== run end: %d total alert(s); %s ===",
                 len(all_alerts), "; ".join(summary_lines))
    # Always exit 0. Per-site fetch failures are normal transient noise
    # at 1-min cadence; logging them is enough. Returning non-zero here
    # would cause GitHub Actions to spam failure emails on every blip.
    if any_fetch_failed:
        logging.info("(one or more sites had a transient fetch error; not failing the run)")
    # Dead-man's-switch: ping the healthcheck URL on every completed run.
    # If these pings stop (box dead, cron broken, network down), the
    # healthcheck provider alerts the user from its own infrastructure.
    ping_healthcheck()
    return 0


def ping_healthcheck():
    # Strip whitespace and any stray BOM that can sneak in via secret managers.
    url = os.environ.get("HEALTHCHECK_URL", "").strip().lstrip("﻿").strip()
    if not url.startswith("http"):
        return
    try:
        requests.get(url, timeout=10)
    except Exception as exc:
        logging.warning("healthcheck ping failed: %s", exc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-site drop monitor.")
    parser.add_argument(
        "--test-email", action="store_true",
        help="Send a sample alert email and exit (does not touch state).",
    )
    args = parser.parse_args()
    sys.exit(run_test_email() if args.test_email else main())
