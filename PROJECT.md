# typ3-monitor (multi-site drop monitor)

Polls a configured list of cannabis storefronts every minute and emails on drops, restocks, and new variant options.

## Architecture (as of 2026-05-29)

**Trigger:** Google Cloud Scheduler job `drop-monitor-trigger` (project `drop-monitor-497817`, region `us-central1`, schedule `* * * * *`) POSTs to GitHub `workflow_dispatch` every minute. Free tier (1 of 3 free jobs). Replaced cron-job.org, which silently auto-disabled itself on 2026-05-26 and caused a missed drop.
**Backup trigger:** GitHub Actions native `schedule: */5` left ON for defense-in-depth.
**Runner:** GitHub Actions (public repo `solenyaaaaaaa/typ3-monitor`, unlimited free minutes) runs `monitor.py`.
**Failure detection:** healthchecks.io dead-man's-switch. `monitor.py` pings `HEALTHCHECK_URL` (repo secret) every run; if pings stop ~15 min, healthchecks.io emails shlomotess@gmail.com from its own infra. This is what was missing when cron-job.org died silently.
**Cost:** $0. (A GCP VM was rejected because the required external IP costs ~$3/mo.)

To manage the trigger you need `gcloud` authed as shlomotess@gmail.com — see SYSTEM-MODS.md (2026-05-29 entry) for pause/resume/inspect commands.

## Status — 2026-05-06

- **Multi-site, 8 sites monitored** (config in `config.json`):
  1. **TYP3 Cannabis** (`typ3cannabis.com/store`) — alerts on new product drops and restocks; ignores known merch.
  2. **Hemp Barn — Living Soil** (`thehempbarn.com/product/livingsoil/`) — alerts only on **new strains** whose section of the product short-description contains `special`, `all time`, or `10/10` (case-insensitive). Other new strains are silently logged.
  3. **Hemp Barn — Organic Soil** (`thehempbarn.com/product/organicsoil/`) — same rule as Living Soil: only new strains with `special`, `all time`, or `10/10` in the description.
  4. **Caregiver Pharms** (`caregiverpharms.com/collections/all`) — alerts on new product handles, on products going from all-sold-out → any-variant-available, and on a "Smalls/Micros" variant becoming available on any product.
  5. **Flow Gardens — Smalls** (`flowgardens.com/products/smalls`) — alerts only when a **new Type 2** strain is added to the dropdown (parsed from the strain name's "Type N" suffix). Type 1 / Type 3 / Type 4 / Type 5+ additions are silently logged. Configurable via `flowgardens_smalls.allowed_types` (currently `[2]`).
  6. **Five Leaf Wellness** (`fiveleafwellness.com`) — polls the WooCommerce **Store API** (`/wp-json/wc/store/v1/products`) and alerts when a product whose authoritative `categories` include a target tier (default `top-shelf`; `mid-tier` excluded) newly appears. Tier membership is re-checked every run, so a product categorised Top Shelf *after* it first drops still alerts once. Configurable via `fiveleafwellness.target_categories`.
  7. **Beleafer — Indoor Hemp Flower** (`beleafer.com/product-category/hemp-flower/indoor/`) — alerts only when a new product appears AND its **product summary block** contains a "Type 2" designation. Type detection uses `\btype\s*N\b` regex; the scan is scoped to the WooCommerce `product-summary` block so the site's `blfr.type3` Instagram link in the footer does not false-match. The product page is re-checked every run until it matches or an 8h window lapses (see "Site-wide audit" below), so a Type 2 designation added after the product is first listed is not missed. Configurable via `beleafer_indoor.allowed_types`.
  8. **High Alpine Genetics** (`highalpinegenetics.com`) — Weebly shop mixing seeds and flower in one search listing; alerts when a new **non-seed** product whose name or description contains a `Type N` matching `allowed_types` (default `[1, 2]`) appears. Seeds are detected by name (`seed`/`fem`/`feminized`) and description cues, then suppressed. Re-checks each not-yet-matched product every run (same helper as Beleafer), so a type designation added after listing is caught.
- Cloud workflow runs every 5 min on a fresh Ubuntu runner; commits updated `state.json` back to `main`. Verified green after the multi-site upgrade.
- Local Windows scheduled task: **disabled** (still registered) so cloud is the only sender.
- Email: aggregated, one per run, sectioned by site. Subject prefix `[Drop Monitor]`.

## How it works

`monitor.py` runs through each enabled site once per invocation:

1. **Fetch** site-specific snapshot (HTML scrape for TYP3 + Hemp Barn; Shopify `/products.json` for Caregiver Pharms and Flow Gardens; WooCommerce Store API for Five Leaf Wellness).
2. **Diff** against the previous snapshot stored under `state.sites.{site_name}` in `state.json`.
3. Each adapter emits zero or more typed alerts:
   - `DROP` — TYP3 new in-stock product
   - `RESTOCK` — TYP3 / Caregiver previously unavailable → available
   - `NEW_PRODUCT` — Caregiver new product handle
   - `NEW_STRAIN` — Hemp Barn / Flow Gardens new strain option
   - `SMALLS_BACK` — Caregiver smalls/micros variant going from missing-or-unavailable → available
4. After all sites run, alerts are aggregated into **one email** with a section per site. On Windows it also fires a toast + beep + browser tab per alert.
5. State is saved atomically. First run for any site only seeds — never alerts.
6. One site's failure (network blip, parse error) is logged but does not stop other sites from running.

**Hemp Barn keyword re-check (added 2026-06-17):** the two Hemp Barn sites match each not-yet-alerted strain's description against the keywords (`special`, `all time`, `all-time`, `10/10`) on *every* run, not just the run the strain first appears. A match fires one alert and records the strain in that site's `alerted` set in `state.json`; an unmatched strain keeps being re-checked on later runs. Rationale: the vendor often adds a strain to the dropdown minutes before its description is written, which previously dropped the match silently (e.g. "Diesel Burger" hit the dropdown 2026-06-16 19:47 UTC, its "special" description landed 19:51 UTC, so the first-appearance-only check never saw it). The re-check shipped in "start clean" mode: every strain already on the page when it deployed was seeded as `alerted`, so it did not retro-fire on the existing catalog (Diesel Burger included).

**Five Leaf Wellness rewrite (added 2026-06-19):** switched from scraping product detail pages for the literal words "top shelf" to reading the WooCommerce Store API (`/wp-json/wc/store/v1/products`) and matching each product's authoritative `categories` against `target_categories` (default `top-shelf`). The old scan missed real top-shelf drops because a product's tier renders only as a category link in the site nav (deliberately excluded to avoid false matches), so it was invisible unless those words also happened to appear in the description text; it also never re-checked a product after first sighting, so a product moved into Top Shelf after it first appeared was lost. The new adapter re-checks category membership every run (late-categorised drops still alert), tracks a per-site `alerted` set, and deployed start-clean (current top-shelf members seeded as `alerted`, old keyword-matched products carried forward). Note: this site has no `top-tier` category, so the old `top tier` keyword matched nothing. **Site-wide audit + shared re-check helper (added 2026-06-19):** every adapter was audited for the "evaluate once at first sighting, never re-check" bug. **Safe as-is:** TYP3 (re-evaluates stock each run; a new-but-sold-out product is caught by the RESTOCK path), Caregiver (new products always alert; stock/smalls are re-evaluated transitions), Flow Gardens (the Type-N discriminator is part of the strain-name identity key, so a relabel is a new entry that re-evaluates). **Fixed:** Beleafer and High Alpine both scanned a separately-fetched detail page once and never again. Both now use a shared `recheck_listing_diff()` helper in `monitor.py` that re-checks each not-yet-matched product every run until it matches (alert once; tracked via a per-URL `alerted` flag under a `v:2` state schema) or an 8h `recheck_window_hours` lapses, with a per-run fetch cap (`DEFAULT_MAX_RECHECKS_PER_RUN`, 25) as a load guard. Deployed start-clean (current listings seeded as handled). High Alpine is Weebly (no Store API), so unlike Five Leaf it cannot move to category membership and relies on the re-check instead.

**Email delivery hardening (added 2026-06-19):** `email_alerts()` now retries the SMTP send up to 3 times (`email_retry_attempts`, exponential backoff) before giving up, so a transient Gmail/network hiccup no longer drops a run's alerts. A live `test-email` workflow run on 2026-06-19 confirmed end-to-end delivery to `EMAIL_TO`. **Persistent-failure notification:** if all retries fail, the run pings the healthcheck's `/fail` endpoint (`ping_healthcheck(success=False)`), so healthchecks.io flags the check down and notifies the user from its own infrastructure - independent of the SMTP that just failed - within that run. The undelivered alert is not re-sent (state still advances), so the notification is the recovery signal to go check the logs/site; a later healthy run pings the base URL and clears the check. Needs `HEALTHCHECK_URL` set (it is). `email_alerts()` returns a delivered/failed bool that `main()` feeds to `ping_healthcheck`. Historical note: a product matched during local development and committed into the initial `state.json` (Five Leaf "Super Boof") was never emailed, because the live cloud monitor saw it already in the seed and never treated it as new.

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
