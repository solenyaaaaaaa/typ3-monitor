# typ3-monitor (multi-site drop monitor)

Polls a configured list of cannabis storefronts every minute and emails on drops, restocks, and new variant options.

## Architecture (as of 2026-08-31)

**Trigger:** none. The workflow triggers itself. `.github/workflows/poll.yml` runs a single job that polls in a loop (`.github/scripts/poll_loop.sh`, 5h30m at 60s intervals) and dispatches its own successor as it exits (`.github/scripts/handoff.sh`). `workflow_dispatch` is a documented exception to GitHub's rule that `GITHUB_TOKEN` cannot trigger workflows, so the handoff needs no PAT and there is no credential to expire. Requires `permissions: actions: write`.
**Watchdog:** GitHub Actions native `schedule: */5` restarts the chain if a run dies without handing off. It is NOT the primary trigger and must never be relied on as one - measured 2026-08-28..31, GitHub fired it 17 times in 3 days with gaps of 2h to 7.6h. It earns its place because it needs no token, so it cannot fail for the same reason the handoff would.
**Concurrency:** group `drop-monitor-loop`, `cancel-in-progress: false`. Exactly one loop alive; a watchdog firing mid-loop queues instead of killing it, and GitHub starts the queued run the moment the loop exits, which doubles as a second handoff path. Do NOT set `cancel-in-progress: true` - that is the config that caused "No jobs were run" failure emails in May.

**Why no external scheduler:** the every-minute trigger died twice, both times because a free third party went away silently. cron-job.org auto-disabled itself 2026-05-26. Google Cloud Scheduler (`drop-monitor-trigger`, project `drop-monitor-497817`) stopped 2026-08-28 17:49 UTC when the GCP free trial expired, and went unnoticed for 3 days at 0.40% coverage. Its logs showed dispatches returning 204 right up to the end, so nothing was misconfigured - the service was simply switched off underneath it. Adding a better third-party scheduler would not fix the class of failure; removing the dependency does.
**Runner:** GitHub Actions (public repo `solenyaaaaaaa/typ3-monitor`, unlimited free minutes) runs `monitor.py`.
**Failure detection:** healthchecks.io dead-man's-switch. `monitor.py` pings `HEALTHCHECK_URL` (repo secret) every run; if pings stop ~15 min, healthchecks.io emails shlomotess@gmail.com from its own infra. This is what was missing when cron-job.org died silently.
**Cost:** $0. (A GCP VM was rejected because the required external IP costs ~$3/mo.)

Nothing outside GitHub needs managing. To pause polling, disable the workflow; to resume, re-enable it and dispatch it once to restart the chain (the watchdog would also restart it, just slower). The old Cloud Scheduler job and its GCP project are no longer used.

## Status — 2026-05-06

- **Multi-site, 9 sites monitored** (config in `config.json`):
  1. **TYP3 Cannabis** (`typ3cannabis.com/store`) — alerts on new product drops and restocks; ignores known merch.
  2. **Hemp Barn — Living Soil** (`thehempbarn.com/product/livingsoil/`) — alerts only on **new strains** whose section of the product short-description contains `special`, `all time`, or `10/10` (case-insensitive). Other new strains are silently logged.
  3. **Hemp Barn — Organic Soil** (`thehempbarn.com/product/organicsoil/`) — same rule as Living Soil: only new strains with `special`, `all time`, or `10/10` in the description.
  4. **Caregiver Pharms** (`caregiverpharms.com/collections/all`) — **narrowed 2026-09-10 to two alerts only**: `SMALLS_BACK` when an ounce of smalls/micros becomes available on any product, and `NEW_VARIANT` when a size option appears that the shop has NEVER offered on any product. Ordinary restocks of existing sizes are silent, and a brand-new STRAIN is silent too — only a never-before-seen SIZE counts as new here. The known-size vocabulary lives in `state.sites.caregiverpharms.known_variant_titles` and is normalised (lowercased, abbreviation periods dropped, decimals kept) because the shop writes the same size both "1 oz (28g) Smalls and Micros" and "1 Oz. (28g) Smalls and Micros"; without that a re-spelling would read as a new size.
  5. **Flow Gardens — Smalls** (`flowgardens.com/products.json`) — **rewritten 2026-09-09**. The shop split its single `smalls` handle into one product per type on 2026-08-12, so the type now lives in the PRODUCT title, not the strain name, and a watched type may have no product at all. Scans the whole catalog for smalls/smallz products ("Small Batch" and "Assorted Small Buds" excluded) and alerts both when a smalls product of a watched type is listed and when a new strain is added to one. Configurable via `flowgardens_smalls.allowed_types` (currently `[2]`; there is no Type 2 Smallz product as of 2026-09-09).
  6. **Five Leaf Wellness** (`fiveleafwellness.com`) — polls the WooCommerce **Store API** (`/wp-json/wc/store/v1/products`) and alerts when a product whose authoritative `categories` include a target tier (default `top-shelf`; `mid-tier` excluded) newly appears. Tier membership is re-checked every run, so a product categorised Top Shelf *after* it first drops still alerts once. Configurable via `fiveleafwellness.target_categories`.
  7. **Beleafer — Indoor Hemp Flower** (`beleafer.com/product-category/hemp-flower/indoor/`) — alerts only when a new product appears AND its **product summary block** contains a "Type 2" designation. Type detection uses `\btype\s*N\b` regex; the scan is scoped to the WooCommerce `product-summary` block so the site's `blfr.type3` Instagram link in the footer does not false-match. The product page is re-checked every run until it matches or an 8h window lapses (see "Site-wide audit" below), so a Type 2 designation added after the product is first listed is not missed. Configurable via `beleafer_indoor.allowed_types`.
  8. **High Alpine Genetics** (`highalpinegenetics.com`) — **DISABLED 2026-09-09**: the shop migrated Weebly to Shopify and now sells seeds only, and this adapter suppresses seeds, so it could never alert. Re-enable only if the shop carries flower again; see `disabled_reason` in `config.json`. Historical behaviour: Weebly shop mixing seeds and flower in one search listing; alerts when a new **non-seed** product whose name or description contains a `Type N` matching `allowed_types` (default `[1, 2]`) appears. Seeds are detected by name (`seed`/`fem`/`feminized`) and description cues, then suppressed. Re-checks each not-yet-matched product every run (same helper as Beleafer), so a type designation added after listing is caught.
  9. **Antheia Farms** (`antheiafarms.myshopify.com`) — Shopify collection (`/collections/all/products.json`); alerts when a product becomes purchasable: an existing product going sold-out → available (restock/drop), or a brand-new product appearing already in stock. All products are sold out ahead of the scheduled ~Aug 22 drop, so the first run seeds and an availability transition fires later. A new sold-out product is recorded silently and alerts when it flips available. Added 2026-06-22.
- Cloud workflow runs every 5 min on a fresh Ubuntu runner; commits updated `state.json` back to `main`. Verified green after the multi-site upgrade.
- Local Windows scheduled task: **disabled** (still registered) so cloud is the only sender.
- Email: aggregated, one per run, sectioned by site. Subject prefix `[Drop Monitor]`.

## How it works

`monitor.py` runs through each enabled site once per invocation:

1. **Fetch** site-specific snapshot (HTML scrape for TYP3 + Hemp Barn; Shopify `/products.json` for Caregiver Pharms, Flow Gardens, and Antheia Farms; WooCommerce Store API for Five Leaf Wellness).
2. **Diff** against the previous snapshot stored under `state.sites.{site_name}` in `state.json`.
3. Each adapter emits zero or more typed alerts:
   - `DROP` — TYP3 new in-stock product
   - `RESTOCK` — TYP3 previously unavailable → available (Caregiver no longer emits this; see site 4)
   - `NEW_PRODUCT` — Five Leaf / Beleafer / Flow Gardens new qualifying product (Caregiver no longer emits this)
   - `NEW_STRAIN` — Hemp Barn / Flow Gardens new strain option
   - `SMALLS_BACK` — Caregiver ounce of smalls/micros becoming available
   - `NEW_VARIANT` — Caregiver size option never offered on any product before
4. After all sites run, alerts are aggregated into **one email** with a section per site. On Windows it also fires a toast + beep + browser tab per alert.
5. State is saved atomically. First run for any site only seeds — never alerts.
6. One site's failure (network blip, parse error) is logged but does not stop other sites from running. Three consecutive real fetch/diff failures for the same enabled site mark the run unhealthy and send Healthchecks a `/fail` ping with a concise diagnostic in the run log. A successful fetch and diff clears that site's counter; intentional `min_interval` skips do neither. SMTP delivery failure is immediately unhealthy. Healthcheck responses are HTTP-status checked.

**Hemp Barn keyword re-check (added 2026-06-17):** the two Hemp Barn sites match each not-yet-alerted strain's description against the keywords (`special`, `all time`, `all-time`, `10/10`) on *every* run, not just the run the strain first appears. A match fires one alert and records the strain in that site's `alerted` set in `state.json`; an unmatched strain keeps being re-checked on later runs. Rationale: the vendor often adds a strain to the dropdown minutes before its description is written, which previously dropped the match silently (e.g. "Diesel Burger" hit the dropdown 2026-06-16 19:47 UTC, its "special" description landed 19:51 UTC, so the first-appearance-only check never saw it). The re-check shipped in "start clean" mode: every strain already on the page when it deployed was seeded as `alerted`, so it did not retro-fire on the existing catalog (Diesel Burger included).

**Five Leaf Wellness rewrite (added 2026-06-19):** switched from scraping product detail pages for the literal words "top shelf" to reading the WooCommerce Store API (`/wp-json/wc/store/v1/products`) and matching each product's authoritative `categories` against `target_categories` (default `top-shelf`). The old scan missed real top-shelf drops because a product's tier renders only as a category link in the site nav (deliberately excluded to avoid false matches), so it was invisible unless those words also happened to appear in the description text; it also never re-checked a product after first sighting, so a product moved into Top Shelf after it first appeared was lost. The new adapter re-checks category membership every run (late-categorised drops still alert), tracks a per-site `alerted` set, and deployed start-clean (current top-shelf members seeded as `alerted`, old keyword-matched products carried forward). Note: this site has no `top-tier` category, so the old `top tier` keyword matched nothing. **Site-wide audit + shared re-check helper (added 2026-06-19):** every adapter was audited for the "evaluate once at first sighting, never re-check" bug. **Safe as-is:** TYP3 (re-evaluates stock each run; a new-but-sold-out product is caught by the RESTOCK path), Caregiver (new products always alert; stock/smalls are re-evaluated transitions), Flow Gardens (the Type-N discriminator is part of the strain-name identity key, so a relabel is a new entry that re-evaluates). **Fixed:** Beleafer and High Alpine both scanned a separately-fetched detail page once and never again. Both now use a shared `recheck_listing_diff()` helper in `monitor.py` that re-checks each not-yet-matched product every run until it matches (alert once; tracked via a per-URL `alerted` flag under a `v:2` state schema) or an 8h `recheck_window_hours` lapses, with a per-run fetch cap (`DEFAULT_MAX_RECHECKS_PER_RUN`, 25) as a load guard. Deployed start-clean (current listings seeded as handled). High Alpine is Weebly (no Store API), so unlike Five Leaf it cannot move to category membership and relies on the re-check instead.

**Email delivery hardening (added 2026-06-19):** `email_alerts()` now retries the SMTP send up to 3 times (`email_retry_attempts`, exponential backoff) before giving up, so a transient Gmail/network hiccup no longer drops a run's alerts. A live `test-email` workflow run on 2026-06-19 confirmed end-to-end delivery to `EMAIL_TO`. **Persistent-failure notification:** if all retries fail, the run pings the healthcheck's `/fail` endpoint (`ping_healthcheck(success=False)`), so healthchecks.io flags the check down and notifies the user from its own infrastructure - independent of the SMTP that just failed - within that run. The undelivered alert is not re-sent (state still advances), so the notification is the recovery signal to go check the logs/site; a later healthy run pings the base URL and clears the check. Needs `HEALTHCHECK_URL` set (it is). `email_alerts()` returns a delivered/failed bool that `main()` feeds to `ping_healthcheck`. Historical note: a product matched during local development and committed into the initial `state.json` (Five Leaf "Super Boof") was never emailed, because the live cloud monitor saw it already in the seed and never treated it as new.

**Generic health reporting (2026-09-09):** a site becomes unhealthy after three consecutive attempted fetch or diff failures; each persistent-failure run pings Healthchecks `/fail`, while a successful fetch and diff clears that site's counter and a healthy run uses the normal heartbeat. Intentional `min_interval` skips preserve the existing counter but do not increment or clear it. Disabled sites are removed from health accounting. SMTP delivery failures remain immediately unhealthy. Heartbeat HTTP status is validated and diagnostics never log the heartbeat URL.

## Files

- `monitor.py` — main script
- `config.json` — user-editable settings (URL, ignore list, alert toggles, email recipient)
- `state.json` — catalog snapshot (created on first run; do not edit by hand)
- `monitor.log` — append-only run log
- `run.bat` — manual runner (uses `python.exe` so console output is visible)
- `requirements.txt` — `requests`, `winotify`
- `%APPDATA%\typ3-monitor\secrets.json` — SMTP credentials (Gmail App Password). Outside the project folder so it does not get synced by OneDrive.

## Cloud schedule (active)

- Repo: `solenyaaaaaaa/typ3-monitor` (private)
- Workflow: `.github/workflows/poll.yml`
- Trigger: cron `*/5 * * * *` (every 5 min) plus `workflow_dispatch` for manual runs
- Runner: `ubuntu-latest`
- Secrets: `SMTP_USER`, `SMTP_PASSWORD` (encrypted, set via `gh secret set`)
- State persistence: each run commits `state.json` back to `main` with message `state: update [skip ci]`
- Free-tier usage: ~12 sec/run × 288 runs/day = ~58 min/day = well within the 2,000 free min/month for private repos

Manage from any shell with `gh` installed:
- Trigger poll now: `gh workflow run poll.yml`
- Send test email: `gh workflow run test-email.yml`
- List recent runs: `gh run list --workflow=poll.yml --limit 10`
- View a run's logs: `gh run view <id> --log`
- Pause cloud schedule: `gh workflow disable poll.yml`
- Resume: `gh workflow enable poll.yml`

GitHub Actions UI: https://github.com/solenyaaaaaaa/typ3-monitor/actions

## Local scheduled task (disabled, kept as fallback)

- Name: `TYP3 Drop Monitor`. Currently `State: Disabled` so it does not fire.
- If you ever want to fall back to local-only polling: disable the cloud workflow first (`gh workflow disable poll.yml`), then `Enable-ScheduledTask -TaskName "TYP3 Drop Monitor"`. Running both at once causes duplicate emails.
- Remove entirely: `Unregister-ScheduledTask -TaskName "TYP3 Drop Monitor" -Confirm:$false`.

## Tweaking behavior

Edit `config.json` in the repo (commit + push to take effect on cloud runs). Top-level keys:

- `email_enabled`, `email_to`, `email_subject_prefix` — email config
- `show_toast` / `play_sound` / `open_browser_on_alert` — Windows-only desktop alert channels (no-ops on the Linux runner)
- `user_agent`, `timeout_sec` — HTTP defaults applied to every site

Per-site config blocks (`typ3`, `hempbarn_livingsoil`, `caregiverpharms`, `flowgardens_smalls`) each have:

- `enabled: true|false` — toggle the site on/off
- Site-specific URLs / parsing parameters
- `typ3.ignore_handles` — slugs that never trigger alerts (all known merch + `test-payment`)
- `caregiverpharms.smalls_keywords` — substrings used to identify the "smalls/micros" variant (default: `["smalls", "micros"]`)

Adding a new site = a new adapter class in `monitor.py` + an entry in `SITE_CLASSES` + a config block. Each site implements `fetch()` and `diff()`.

Send a one-off test email anytime:
```
gh workflow run test-email.yml -R solenyaaaaaaa/typ3-monitor   # from cloud
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" monitor.py --test-email   # from this PC
```

If you ever rotate the Gmail App Password: update both `%APPDATA%\typ3-monitor\secrets.json` (local) and the GitHub repo secret (`gh secret set SMTP_PASSWORD --body <new>`).

## Design invariant: never aggregate availability across variants (2026-08-07)

A per-product "is anything in stock" boolean cannot detect a restock. If a
product keeps even one slow-moving variant permanently in stock, the aggregate
never goes False, so it can never transition back to True, so no RESTOCK ever
fires — no matter how many other sizes sell out and return.

This shipped as a real miss. On 2026-08-06 23:53 UTC, Caregiver Pharms dropped
Blueberry Chem #7 and #23 one second apart. #7 alerted; #23 did not. #7 had gone
fully sold out earlier that day, so its aggregate flag had a False to return
from. #23 had held its `1 Oz. (28g) Baller Ounce - All Top Colas` variant in
stock continuously since at least 08-01 (verified across 291 sampled `state.json`
commits), so its 3.5g / 14g / 28g restock was invisible. Four of the six products
in that feed carried an in-stock Baller Ounce and were equally blind.

`CaregiverPharmsSite` and `AntheiaFarmsSite` now store `variants: {title: bool}`
and alert when any individual variant goes sold-out to available, naming the
sizes. `any_available` is retained only for NEW_PRODUCT wording.

Rules for any future site class:
- Alert on the finest-grained availability signal the feed exposes. Never
  `any()` a list of variants into one boolean and diff that.
- When the shape of a stored state entry changes, seed the new field on first
  sight without alerting, or the deploy emits an alert for every currently
  available item. Both classes above do this by treating a missing `variants`
  key as "seed silently this run."
- Keep dedicated alert kinds (e.g. `SMALLS_BACK`) out of the generic per-variant
  path so one event does not emit two emails.

## Design invariant: chunk boundaries must not depend on the current catalog (2026-08-25)

Hemp Barn descriptions are one block of `<p><strong>Strain (genetics)</strong>text</p>`
paragraphs. The parser split that block by looking for the headings of strains
currently in the variations dropdown, and ran each chunk to the next such
heading. That is wrong whenever the page contains a heading the dropdown does
not: a strain that sells out leaves the dropdown while its heading stays on the
page, so it stops acting as a boundary and the strain above it swallows its
write-up.

That shipped a false positive. On 2026-08-25 "Snozzberries" alerted with
`matched keyword "10/10"`. The 10/10 belongs to G-Chem, which had sold out and
left the dropdown; Snozzberries' parsed description was 443 characters instead
of its real 200 and ended inside G-Chem's paragraph. G-Chem itself had alerted
correctly weeks earlier while still in the dropdown, which is why the corruption
only surfaced after it sold out.

Boundaries are now every paragraph-leading `<strong>`, independent of the
dropdown. A heading matching no current strain is a boundary that yields no
description. Bold used mid-sentence is not a boundary.

Rules for any future parser:
- Derive chunk boundaries from the document's own structure, never from a list
  of things you currently care about. The page will always contain more than
  your catalog.
- A value parsed this run is authoritative for anything currently on the page.
  Do not merge stale text over it, or a bug fixed in the parser lives on in
  `state.json` and keeps firing.
- If a parse yields nothing, skip evaluation for that run rather than falling
  back to stored values. A run that read nothing must not be able to alert.
- Test with a fixture that includes a heading absent from the catalog and bold
  text used mid-sentence. Both are in the real page and both broke this parser.

## Design invariant: a failure must never look like "no change" (2026-09-09)

A health check found THREE sites monitoring nothing. Two raised errors and were
found the moment per-site failure tracking was added. The third raised nothing
at all and had been dark for 82 days.

| Site | Dark for | Cause |
|---|---|---|
| Flow Gardens | 28 days | shop split its one `smalls` handle into per-type products 2026-08-12; pinned handle 404ed |
| Beleafer | 17 hours | Cloudflare began fingerprint-blocking every request, robots.txt included |
| High Alpine | **82 days** | shop moved Weebly to Shopify; listing URL 404ed and the pagination loop swallowed it |

High Alpine is the one that matters. Its pagination loop treated ANY 404 as
"no more pages", so page 1 failing returned an empty result set. That diffs to
"no change": no exception, no failure counter, `last_polled_at` refreshing every
minute, health green. All 241 stored products still carried
`first_seen: 2026-06-19`, which is the only reason it was detectable at all.

Rules for any adapter that fetches a listing:
- **A 404 on page 1 is a failure. Only page >= 2 ends pagination.** Reusing one
  sentinel for "done paginating" and "the URL is gone" is what created the
  82-day blind spot.
- **A listing that parses to zero products is a failure, not an empty shop.**
  Raise, so the health counter sees it. An empty parse otherwise diffs to
  "no change" forever.
- **Never swallow an exception into an empty return value.** The per-site
  failure counter only sees what raises; anything caught internally is
  invisible to every alerting path.
- **Do not pin a single product handle.** Watch the catalog and filter. A shop
  can restructure at any time, and the thing being waited for may not exist
  yet - there was no Type 2 Smallz product on the day this was rewritten.
- A WAF 403 is about the TLS/HTTP2 fingerprint, not the User-Agent. `http_get`
  retries once with `curl_cffi` impersonation; do NOT forward our own UA on
  that retry or the mismatch keeps the 403.

Staleness is the check that found all three: compare each site's
`last_polled_at`/`first_seen` against now. That catches any reason a site stops
updating, including reasons that raise nothing. Worth running by hand when
anything seems off.

## Design invariant: bound every external call inside the long-lived loop (2026-09-10)

The loop runs 5h30m. Anything inside it that can block forever takes the whole
loop with it, and the job keeps reporting `in_progress` and healthy the entire
time. Run 157443 wedged on ONE iteration for 17 minutes and committed nothing;
an identical run takes ~21s locally, so nothing was merely slow. Cadence went
from a clean 60s to zero with no error anywhere.

- `python monitor.py` runs under `timeout --kill-after=15s 150`.
- `git push` / `git fetch` in `push_state` run under `timeout 90`.
- Timeouts are counted separately from ordinary non-zero exits and reported in
  the end-of-loop summary, so "wedged" and "failed" stay distinguishable.

**Do not force a code changeover by dispatching a run and cancelling the live
one.** That race is the likely source of this repo's permanently stuck runs
(84294, 90164, 143238, 143301 are all still queued/in_progress from earlier
months). If a changeover is genuinely urgent: cancel first, WAIT for the run to
reach a terminal state, then dispatch. Otherwise just let the handoff carry the
new code at the next loop boundary.

## Alerting policy: the check answers one question (2026-09-10)

The Healthchecks check means "the monitor is running and can tell me about a
drop". Nothing else may flip it. Previously any persistent site failure did, so
each time Beleafer's Cloudflare block came and went the user got a DOWN mail
and then an UP mail. Over one 26-hour window the runner never missed a beat -
1,544 polls at ~1/min - while the check flapped purely on Beleafer. That is how
you teach someone to ignore the one alert that matters.

- `unhealthy` (the `/fail` ping) is set ONLY by undelivered alert email. Absence
  of any ping still covers "the monitor is dead" - that is the dead-man's-switch
  doing its actual job.
- Site trouble is recorded in `state.health.site_failures` with
  `first_failed_at`, and a site is marked in `state.health.degraded_sites` only
  after `site_degraded_after_minutes` (default 120) of CONTINUOUS failure.
  `count` is capped at the threshold and cannot distinguish 3 minutes from 3
  days, so duration is the thing to reason about.
- `monitor.py` exits non-zero only for undelivered email. The poll loop reads
  that exit code, so a broken store must not fail the job.

**Who notices site breakage now:** `triage.py` plus the `drop-monitor-triage`
scheduled task (every 4h, silent when healthy) - see SYSTEM-MODS.md. Decoupling
without that replacement would recreate the silent-failure problem that hid High
Alpine for 82 days. Do not remove one without the other.

**Beleafer is expected to flap.** Cloudflare blocks GitHub's runner IPs
intermittently and the block is not defeated by fingerprint impersonation, which
only helps when the block keys on the client rather than the address. It
recovers on its own. Escalate it only after many continuous hours.

## Known caveats

- **GitHub Actions cron timing is not exact.** Scheduled workflows can be delayed up to ~10–15 min during peak GitHub load. Average is much closer to 5 min. For drop monitoring this trades worst-case timing slippage for 24/7 coverage that does not depend on this PC.
- Toast / sound / browser pop alerts no longer fire — those were Windows-only and the cloud runner is Linux. Email is the sole notification channel now. If you want desktop alerts back when you are at the PC, re-enable the local scheduled task (and disable the cloud one to avoid duplicates).
- The site is a custom Next.js app, not stock Shopify. The HTML markers (`sold-out-card`, `data-testid="product-title"`) are stable for now but could change. If the workflow log ever shows `no products parsed; site format may have changed - keeping prior state`, the parser regex in `monitor.py` needs an update.
- The cloud commits `state.json` back to `main` every time the catalog changes. Your local `git pull` will fast-forward those commits in. The OneDrive copy of `state.json` will only get refreshed if you `git pull`.
- App Password security: the 16-char password is stored encrypted at rest in GitHub Secrets and only injected as an env var into the runner. It cannot be read back via `gh secret get`. To rotate, generate a new App Password at https://myaccount.google.com/apppasswords and re-run `gh secret set SMTP_PASSWORD --body <new>`.

## Resuming work in a new session

If a future session opens this project: cloud polling is the source of truth. Health check sequence:
1. `gh run list --workflow=poll.yml --limit 5` — last few runs should all be green ✓.
2. If any failed, `gh run view <id> --log-failed` to see the error.
3. If parse errors appear (`no products parsed`), the site HTML changed — update regexes in `monitor.py`, commit, push.
4. The local scheduled task is intentionally disabled; do not re-enable unless you also disable the cloud workflow first.
