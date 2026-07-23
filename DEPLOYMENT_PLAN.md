# Granjur — Server Deployment & Multi-Bot Scraping Plan

> Goal: run the pipeline unattended on a Linux server as a **fleet of independent, parallel bots**
> (one per source), each scheduled by its own systemd timer, each governed by shared rate limits and
> **rest/backoff triggers** so we never get **flagged, rate-limited, or IP-blacklisted** on any source —
> and never burn the Gmail sending reputation.
>
> Decisions locked in: **Linux (systemd timers)** · **Fiverr/Upwork = CSV lane (no scraping)** ·
> **Gmail now, custom-domain-ready seams** · **Google Maps = direct scrape with a 5-layer safety trigger**.

---

## 1. The fleet at a glance

Each **source = one independent bot** = its own systemd `.timer` + `.service` + governor bucket. They run
**concurrently**; none blocks another. All discovery bots funnel through `intake.submit()`, which
**dedupes by domain** — so two bots finding the same company is harmless. Everything communicates only
through the lead `status` column in Postgres (the existing decoupled design).

```
   ┌──────────────── DISCOVERY BOTS (parallel, independent timers) ─────────────────┐
   │  bot-gmaps    bot-osm    bot-remoteok    bot-remotive    bot-csv                │
   │  (scrape)     (API)      (API)           (API)           (folder ingest)        │
   │      │           │            │              │               │                 │
   │      └───────────┴──── intake.submit() → DEDUPE BY DOMAIN ───┴─────────────────┘
   └──────────────────────────────────┬─────────────────────────────────────────────┘
                                       ▼   leads land as DISCOVERED
        bot-enrich → bot-qualify → bot-send → bot-followup      (processing bots)
              │                        ▲
              │        bot-reply-scan, bot-health, bot-export   (support bots)
              ▼
   ┌───────────────────────────────────────────────────────────────────────────────┐
   │  rate_governor  (shared, DB-backed)                                             │
   │   • per-bot token buckets: per-run / per-hour / per-day counts                  │
   │   • rest_until per bot (min-interval + backoff after a block)                   │
   │   • fail_streak → exponential backoff; consecutive-block circuit breaker        │
   └───────────────────────────────────────────────────────────────────────────────┘
```

**The 7 rate-bearing bots** (the ones that talk to an external service and can be flagged — each has an
explicit trigger set in §4): `bot-gmaps`, `bot-osm`, `bot-remoteok`, `bot-remotive`, `bot-enrich`,
`bot-send`, `bot-reply-scan`. The rest (`bot-qualify`, `bot-followup`, `bot-health`, `bot-export`,
`bot-csv`) either run locally or share the send budget and are covered too.

---

## 2. The rate governor (shared safety spine)

One small module every bot calls **before** doing work and **after** each unit of work. It is the single
place where all limits and rests live, so behaviour is consistent and tunable without touching bot code.

**State** — one row per bot in a new `rate_state` table:

| column | meaning |
|---|---|
| `source` (PK) | bot name, e.g. `gmaps`, `osm`, `send` |
| `run_count`, `run_start` | per-**run** window (reset each tick) |
| `hour_count`, `hour_start` | per-**hour** rolling window |
| `day_count`, `day_start` | per-**day** rolling window (the real ceiling) |
| `rest_until` | if `now < rest_until`, the bot no-ops and exits 0 |
| `fail_streak` | consecutive blocks → drives exponential backoff |
| `blocks_today`, `blocks_day_start` | for the consecutive-block circuit breaker |

**API** (`scripts/governor.py`):

- `can_run(source)` → `False` if resting, or hour/day cap hit. Bot exits cleanly (this *is* the rest).
- `record(source, n=1)` → bump counters, roll windows.
- `back_off(source, hours_range)` → set `rest_until = now + rand(hours_range) * 2^fail_streak` (capped),
  `fail_streak += 1`, `blocks_today += 1`. Called on any block signal.
- `reset_fail(source)` → clear `fail_streak` on a clean run.
- `tripped(source)` → `True` if `blocks_today >= breaker_limit` (park the bot 24 h).

**Why DB-backed, not an in-process counter:** cron fires each bot many times a day in **separate
processes**. A per-run Python counter resets every tick, so `20/run × N runs = uncapped/day`. The daily
ceiling and the rest clock must **persist across runs** — that is the whole point of the governor.

---

## 3. The 5-layer safety trigger (applies to every scraping bot; tuned tightest for Maps)

```
Layer 1  PER-RUN cap       in-process counter → stop THIS session at MAX_SCRAPE_PER_RUN
Layer 2  PER-DAY cap       governor day_count → stop ALL runs today at MAX_SCRAPE_PER_DAY
Layer 3  MIN-INTERVAL      governor rest_until → next cron tick no-ops if < interval since last run
Layer 4  BLOCK DETECTOR ★  CAPTCHA / consent-wall / /sorry/ / 429 → STOP NOW + rest (count-blind)
Layer 5  RANDOM DELAYS     sleep(random 5–12s) between actions + randomized session length/scrolls
```

**Layer 4 is the primary defense.** Layers 1–3 cap *volume*; layer 4 reacts the instant the source signals
it noticed us — you can be challenged at lead #3, long before any counter trips. A count-only stop keeps
scraping straight into a ban. See §5 for exactly how the block detector decides.

---

## 4. Per-bot cron specifications & anti-flag triggers

> Times use systemd `OnCalendar`. All discovery/send bots additionally respect **business-hours-only**
> gating where noted. Every value below is a governor knob (env-overridable), not a magic number in code.

### 4.1 🗺️ bot-gmaps — Google Maps (direct scrape) — HIGHEST RISK
- **Method:** headless browser (Playwright + stealth) → `google.com/maps/search/<niche>+in+<city>`,
  scroll the results feed, extract name → website → phone. Email comes later via `bot-enrich`.
- **Timer:** `20 8-18/2 * * *` — **one session every 2 h, business hours only** (low & slow is the defense).
- **Per-run slice:** **1 city cell**, scroll to ~15–20 cards, keep `PER_CELL` new after dedup.
- **Triggers (the 5 layers):**
  | Knob | Value |
  |---|---|
  | `GMAPS_MAX_PER_RUN` (Layer 1) | **20** |
  | `GMAPS_MAX_PER_DAY` (Layer 2) | **30** |
  | min interval (Layer 3) | **2 h** |
  | random delay (Layer 5) | **5–12 s** between scrolls/cards + jittered session length |
  | block → rest (Layer 4) | **6–12 h**, exponential on repeats |
  | consecutive-block breaker | **3 blocks/day → park 24 h** |
- **Infra prerequisites (unique to this bot):** rotating **residential proxies** (datacenter IPs get
  CAPTCHA'd fast), **Playwright** (Maps is JS-rendered — `requests` won't work), egress **isolated from
  SMTP** (a burned scraper IP must not touch anything else). Note: this is where "100% free" breaks — a
  small paid proxy pool is effectively required for the scrape to function.
- **Honest risk note:** Maps scraping is ToS-adverse and the most likely thing here to get an IP flagged.
  The safety stack keeps it survivable, not risk-free. Keep the daily cap tiny.

### 4.2 🌍 bot-osm — OpenStreetMap (free API) — LOW RISK
- **Method:** Nominatim geocode → Overpass query (no key), through `collect_osm.py`.
- **Timer:** `0 8-18/1 * * *` — every hour, business hours (offset from gmaps so they don't collide on cities).
- **Per-run slice:** 1 city cell, `FETCH_POOL=90` → shuffle → keep `PER_CELL` new.
- **Triggers:** day cap ~20 cells; **Overpass 429/504 → rest 60 min** (exponential); Nominatim **≤1 req/s**;
  keep the descriptive User-Agent.
- **Required fix:** **cache the geocoded bbox** (new `city_bbox` table / file) instead of re-geocoding the
  same city every run ([`collect_osm.py:42`](wf1_python/collect_osm.py#L42)). Repeated identical Nominatim
  queries over months of ticks are the real OSM-ban risk — caching removes it.

### 4.3 💼 bot-remoteok — RemoteOK jobs (free API) — LOW RISK
- **Method:** `https://remoteok.com/api` JSON; match dev titles, skip recruiters (`collect_jobs.py`).
- **Timer:** `15 */4 * * *` — every 4 h (job feeds refresh slowly; no need to poll hard).
- **Triggers:** ≤6 pulls/day; cap ~30 candidates/pull; **429 → rest 2 h**. Yields company + intent but
  **no domain** → `bot-enrich` resolves contact.

### 4.4 🧑‍💻 bot-remotive — Remotive jobs (free API) — LOW RISK
- **Method:** `remotive.com/api/remote-jobs?category=software-dev` JSON (`collect_jobs.py`).
- **Timer:** `45 */4 * * *` — every 4 h, **offset** from RemoteOK so they don't wake together.
- **Triggers:** same shape as RemoteOK (≤6/day, 429 → rest 2 h).

### 4.5 📥 bot-csv — Fiverr / Upwork / LinkedIn lane (folder ingest) — ZERO RISK
- **Method:** watch an `inbox/` folder; any dropped `*.csv` → existing `wf1.py <csv>` → move to `inbox/done/`.
- **Timer:** `0 * * * *` — hourly.
- **Why:** these platforms ban automated scraping outright. This is how their leads enter **without** our
  server ever touching them — a human/tool exports a CSV, drops it in `inbox/`. No triggers needed.

### 4.6 🔎 bot-enrich — email + tech extraction (site scrape) — LOW/MED RISK
- **Method:** fetch each lead's own site, find public email on Contact/About pages + detect tech
  ([`enrich_free.py`](wf2_python/enrich_free.py)).
- **Timer:** `5,25,45 8-18 * * *` — every 20 min, business hours.
- **Per-run slice:** batch 25 leads.
- **Triggers (new):** **≤2 concurrent** fetches, **0.5 s between site fetches**, per-domain fetch cap so we
  never hammer one host, honest UA. Risk is low because load spreads across many different domains — the
  politeness delay + concurrency cap are the guard. Repeated fetch failures on a domain → skip, don't retry-loop.

### 4.7 ✉️ bot-send — outreach drip (Gmail SMTP) — REPUTATION-CRITICAL
- **Method:** `wf4.py`, but **dripped** — small waves, not one burst.
- **Timer:** `0,20,40 8-18 * * *` — every 20 min, but the existing **per-recipient local-window gate**
  ([`sendwindows.py`](wf3_python/sendwindows.py)) still decides who is actually eligible each tick.
- **Per-run slice:** **3–5 emails**, sent **45–120 s apart with jitter** (today `wf4.py` sends the whole
  batch back-to-back with zero spacing — that burst pattern is the change).
- **Triggers:**
  - **Warmup ramp** is the daily ceiling: 5→10→20→30→40→50/day by mailbox age
    ([`domain_health.py:56`](wf4_python/domain_health.py#L56)); **hard-cap real cold sends at ~40/day** on
    consumer Gmail even once warmed.
  - **Send + follow-up share ONE daily budget** — `domain_health` counts both from `outreach_log`, so they
    can't jointly overshoot.
  - **Auth/blacklist fail (domain health) → refuse to send** (already enforced).
  - **Circuit breaker (new):** bounce rate > ~3% or a spam-complaint spike in a day → auto-pause sending
    till tomorrow (governor rest on `send`).
  - Small **daily-volume variance** (±) and jitter so it's never exactly 40 at the same minutes each day.

### 4.8 Support bots (no external-rate risk, listed for completeness)
| Bot | Timer | Notes |
|---|---|---|
| `bot-qualify` | `10,40 8-18 * * *` | local Ollama LLM pitch — CPU-bound, no external limit |
| `bot-reply-scan` | `*/15 * * * *` | IMAP reply scan → marks REPLIED, **stops that lead's drip** (gentle IMAP) |
| `bot-followup` | `30 9,14 * * *` | due nudges; shares the send day-budget |
| `bot-health` | `30 7 * * *` | domain health + bounce parse; **fail ⇒ pauses send for the day** |
| `bot-export` | `30 23 * * *` | Excel snapshot + central DB; no network |

---

## 5. Block-detector guide — telling a CAPTCHA/consent page from a real results page

The linchpin of Layer 4. Check signals **strongest-first** and stop at the first hit. Never rely on a
single signal; the URL/host check is the most reliable, DOM markers next, HTTP status last.

### 5.1 BLOCKED — stop immediately, `back_off()`
1. **URL / redirect host (strongest):**
   - path contains `/sorry/` → Google's "unusual traffic" interstitial (definitive block).
   - host is `consent.google.com` or page is the "Before you continue" consent wall.
   - redirected to an `accounts.google.com` login/challenge.
2. **DOM / content markers:**
   - a reCAPTCHA present: `iframe[src*="recaptcha"]`, `.g-recaptcha`, `#recaptcha`, or the text
     "I'm not a robot".
   - body text matches (case-insensitive): `unusual traffic`, `detected unusual traffic`,
     `verify you.?re (a )?human`, `are you a robot`, `our systems have detected`.
3. **HTTP status:** `429` (rate-limited) or `403` (forbidden) on the search request.

### 5.2 OK — a genuine results page (proceed)
- URL stays on `google.com/maps/...` (no `/sorry/`, no `consent.`).
- the results **feed** is present: `div[role="feed"]` containing place links `a[href*="/maps/place/"]`.
- at least one card yields a name (and usually a website/phone).

### 5.3 AMBIGUOUS — treat as a soft block (short rest, don't hammer)
- page loads on the right host but the feed selector **never appears** within the wait timeout (could be a
  silent throttle, a slow proxy, or a zero-result city).
- **Rule:** 2 consecutive ambiguous runs on the same proxy → treat as a block (rotate proxy + short rest),
  because a silent throttle looks exactly like this.

### 5.4 Detector contract (pseudocode)
```python
def classify_page(page):
    url = page.url.lower()
    if "/sorry/" in url or "consent.google." in url or "accounts.google.com" in url:
        return "BLOCKED"
    html = page.content().lower()
    if any(s in html for s in ("recaptcha", "unusual traffic", "are you a robot", "not a robot")):
        return "BLOCKED"
    if page.query_selector('div[role="feed"] a[href*="/maps/place/"]'):
        return "OK"
    return "AMBIGUOUS"   # caller: 2-in-a-row on same proxy ⇒ treat as BLOCKED
```
`bot-gmaps` calls this after each navigation; `BLOCKED` → `governor.back_off("gmaps", (6, 12))` + break;
`AMBIGUOUS` twice → rotate proxy + short rest; `OK` → scrape the visible cards then Layer-5 delay.

---

## 6. Custom-domain readiness (Gmail now → own domain later)

All sender identity already reads from env (`GMAIL_ADDRESS`, `GRANJUR_MAILBOX_CREATED`), and
`domain_health` auto-detects shared vs custom domains. So the future move is **config, not a rewrite**:
1. Create a mailbox on your domain (Google Workspace or an ESP).
2. Publish DNS: `SPF` (`v=spf1 include:_spf.google.com ~all`), enable **DKIM** (publish the `google`
   selector), `DMARC` (`v=DMARC1; p=quarantine; rua=mailto:...`). `domain_health` will verify all three.
3. Flip `GMAIL_ADDRESS` (+ app password) and reset `GRANJUR_MAILBOX_CREATED` to restart the warmup ramp.

---

## 7. Deployment mechanics (Linux)

- **Per bot:** a thin entry script `scripts/cron/<bot>.py` that (1) `governor.can_run()`; (2) does one
  bounded slice; (3) `record()` / `back_off()`; (4) exits. Plus a `<bot>.service` (oneshot) + `<bot>.timer`.
- **Overlap protection:** `flock` lockfile per bot so a slow tick never overlaps its own next tick.
- **Secrets:** systemd `EnvironmentFile=` → the project `.env`; never inline secrets in unit files.
- **Staggered `OnCalendar`** (`:00 :05 :10 :15 …`) so bots don't all wake at once and spike the box.
- **Logs:** `journalctl -u granjur-<bot>` per bot; the governor also records last-run/last-block for a
  dashboard panel.

---

## 8. Rollout order (safe)

1. Governor table + `scripts/governor.py`; `GRANJUR_DRY_RUN=1`.
2. `bot-gmaps` (`collect_maps.py`) with the 5-layer trigger + block detector — **run manually, watch logs**.
3. OSM bbox cache + `wf4.py` send jitter.
4. Cron wrappers + systemd timers for all bots; **observe 2–3 days in dry-run** (confirm caps/rests in logs).
5. Custom-domain readiness doc/records prepared.
6. Flip `GRANJUR_DRY_RUN=0` at the lowest warmup rung; let the ramp climb.
