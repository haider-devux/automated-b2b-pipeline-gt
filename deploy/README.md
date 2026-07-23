# Granjur — Linux server deployment (systemd bot fleet)

Runs the pipeline as **12 independent bots**, each its own systemd timer, all governed by the shared
rate governor (`scripts/governor.py`) so nothing gets flagged or blacklisted. Read
[`../DEPLOYMENT_PLAN.md`](../DEPLOYMENT_PLAN.md) for the architecture and per-bot rationale; this is the
hands-on install.

## Go-live sequence (TL;DR — do these in order)
```
ON YOUR MACHINE
 1. commit + push the code to the repo

ON THE SERVER (in this order)
 2. git pull/clone → create venv → pip install -r requirements.txt         (§1)
 3. run the migrations on the SERVER db  (incl. phase7_governor_migration.sql)  (§1)
 4. create .env  (GRANJUR_DRY_RUN=1)  +  set timezone                       (§1)
 5. sudo bash deploy/install.sh        ← core bots start running, in dry-run (§2)
 6. watch 2–3 days: journalctl + python scripts/governor.py                 (§3)
 7. flip GRANJUR_DRY_RUN  1 → 0        ← THIS is go-live                     (§5)
 8. (whenever ready, separately) prove + enable gmaps                       (§4)
```
> **Flip direction:** `GRANJUR_DRY_RUN=1` = safe (bots discover/enrich/qualify for real, but **no email
> sends**); `=0` = live. You **start at 1** and flip **1 → 0** to go live — never the other way.

## 0. Layout assumed by the units
| Thing | Value (edit in `systemd/granjur@.service` if different) |
|---|---|
| Repo path | `/opt/granjur` |
| venv | `/opt/granjur/.venv` (one venv, Linux `bin/python`) |
| Service user | `granjur` |
| Secrets/knobs file | `/opt/granjur/.env` |

## 1. One-time setup
```bash
sudo useradd -r -m -d /opt/granjur granjur          # service account
sudo -u granjur git clone <repo> /opt/granjur
cd /opt/granjur
sudo -u granjur python3 -m venv .venv
sudo -u granjur .venv/bin/pip install -r requirements.txt

# Database (Postgres must exist + be reachable; see the main README for the base schema)
psql -d granjur_pipeline -f database/phase7_governor_migration.sql   # <-- the governor + bbox cache

# Ollama for the pitch writer (bot-qualify)
ollama pull qwen2.5:3b

# Server clock: OnCalendar uses LOCAL time. Pick a sane zone (per-recipient windows are handled in-app).
sudo timedatectl set-timezone UTC

# inbox for the CSV lane (Fiverr/Upwork/LinkedIn)
sudo -u granjur mkdir -p /opt/granjur/inbox
```

Create `/opt/granjur/.env` (chmod 600). **Keep it `KEY=VALUE`** — systemd's `EnvironmentFile` reads it and
the Python phases also self-load it via `python-dotenv`:
```ini
# DB + Gmail (as in the main README)
DB_HOST=localhost
DB_NAME=granjur_pipeline
DB_USER=postgres
DB_PASSWORD=...
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=...
IMAP_HOST=imap.gmail.com

# SAFE BY DEFAULT — nothing is emailed until you flip this to 0
GRANJUR_DRY_RUN=1

# bot-gmaps (Google Maps scraper) — REQUIRED for real use
GMAPS_PROXIES=http://user:pass@resi1:port,http://user:pass@resi2:port   # rotating residential proxies

# (optional) mailbox warmup anchor so the ramp is correct
GRANJUR_MAILBOX_CREATED=2026-07-09
```

## 2. Install the timers
```bash
sudo bash deploy/install.sh          # installs 12 units; enables the 11 CORE bots now,
                                     # leaves bot-gmaps installed but DISABLED (experimental)
systemctl list-timers 'granjur-*'    # confirm the schedule (gmaps won't appear until you enable it)
```
**Tiers:** the 11 core bots (osm, jobs, csv, enrich, qualify, send, followup, reply-scan, health, export)
auto-enable — they're low-risk. **bot-gmaps is experimental**: it scrapes a hostile target and needs a
residential proxy, so it's installed but off until you prove it by hand (§4). It can never affect the
other bots — separate process, separate governor row, separate timer.

## 3. Observe in DRY-RUN for 2–3 days (do NOT skip)
With `GRANJUR_DRY_RUN=1`, discovery/enrichment run for real but **no email is sent** (send bots log only).
```bash
journalctl -u granjur@gmaps.service -f          # watch the highest-risk bot
journalctl -u 'granjur@*' --since today          # everything today
/opt/granjur/.venv/bin/python scripts/governor.py   # per-bot counters, rest_until, blocks_today
```
Confirm in the logs: caps respected, `[SAFETY]` rests firing when expected, no bot looping hot.

## 4. Prove bot-gmaps before enabling it (it ships DISABLED)
It scrapes Google Maps, so it stays off cron until you've verified a proxy works and it extracts cleanly.
```bash
# 1) needs Playwright + a proxy in .env (GMAPS_PROXIES=...)
/opt/granjur/.venv/bin/pip install playwright && /opt/granjur/.venv/bin/playwright install chromium

# 2) run ONE cell by hand and watch the outcome
sudo -u granjur bash -c 'cd /opt/granjur/wf1_python && ../.venv/bin/python collect_maps.py US Austin restaurant'
#    clean -> companies found, gmaps day_count ticks up
#    [SAFETY · Layer 4] BLOCK -> detector caught a CAPTCHA/consent wall + rested; fix the proxy/pacing first
#    empty names/websites -> tune the extraction selectors in collect_maps.py against a live page

# 3) a few clean days with GMAPS_MAX_PER_DAY=5, checking the governor after each run
/opt/granjur/.venv/bin/python scripts/governor.py     # gmaps: blocks_today should stay 0

# 4) only then hand it to cron, and ramp GMAPS_MAX_PER_DAY 5 -> 15 -> 30 over days
sudo systemctl enable --now granjur-gmaps.timer
```
**Kill switch:** `sudo systemctl disable --now granjur-gmaps.timer` stops it instantly without touching any
other bot. (The circuit breaker also auto-parks it 24h after 3 blocks in a day.)

## 5. Go live
```bash
# lowest warmup rung first; the ramp climbs automatically with mailbox age
sudoedit /opt/granjur/.env        # set GRANJUR_DRY_RUN=0
# no restart needed — the next send tick picks it up
```

---

## Env knob reference (all optional; safe defaults shown)
| Knob | Default | Bot | Effect |
|---|---|---|---|
| `GRANJUR_DRY_RUN` | `1` | send/followup | `0` = actually send |
| `GRANJUR_SEND_DRIP` | `5` | send | emails per send tick |
| `GRANJUR_SEND_JITTER_MIN/MAX` | `45`/`120` | send/followup | seconds between real sends |
| `GRANJUR_BOUNCE_RATE_CEIL` | `0.03` | send | bounce rate that trips the breaker |
| `GRANJUR_BOUNCE_MIN_SAMPLE` | `20` | send | min sends before the breaker can trip |
| `GRANJUR_SEND_BOUNCE_PARK_HOURS` | `18` | send | park duration when tripped |
| `GRANJUR_WARMUP_CEIL` | `50` | send | absolute daily send ceiling |
| `GMAPS_MAX_PER_RUN` | `20` | gmaps | Layer 1 per-session cap |
| `GMAPS_MAX_PER_DAY` | `30` | gmaps | Layer 2 daily ceiling |
| `GMAPS_MIN_INTERVAL_H` | `2` | gmaps | Layer 3 min gap between runs |
| `GMAPS_DELAY_MIN/MAX` | `5`/`12` | gmaps | Layer 5 seconds between cards |
| `GMAPS_BLOCK_REST_LOW/HIGH_H` | `6`/`12` | gmaps | Layer 4 backoff on a block |
| `GMAPS_BLOCK_BREAKER` | `3` | gmaps | blocks/day → park 24h |
| `GMAPS_PROXIES` / `GMAPS_PROXY` | — | gmaps | residential proxy pool (required) |
| `OSM_MAX_PER_DAY` | `200` | osm | daily approved-lead ceiling |
| `OSM_BACKOFF_HOURS` | `1` | osm | rest when Overpass mirrors fail |
| `JOBS_MAX_PULLS_PER_DAY` | `6` | remoteok/remotive | pulls/day per feed |
| `JOBS_BACKOFF_HOURS` | `2` | remoteok/remotive | rest on 429/error |
| `ENRICH_FETCH_DELAY` | `0.5` | enrich | seconds between site fetches |
| `GRANJUR_BACKOFF_FACTOR_CAP` | `8` | all | max exponential-backoff multiplier |

## Operations cheat-sheet
```bash
systemctl start granjur@osm.service          # run a bot right now (governor still applies)
systemctl stop granjur-send.timer            # pause a bot (e.g. halt all sending)
systemctl start granjur-send.timer           # resume
journalctl -u granjur@send.service -n 100    # recent output of one bot
python scripts/governor.py                   # inspect caps/rests/blocks
```
To manually clear a rest (e.g. after fixing a Maps block), in `psql`:
`UPDATE rate_state SET rest_until=NULL, fail_streak=0, blocks_today=0 WHERE source='gmaps';`

## Loopholes closed (and the ones you still own)
Closed in code: per-run + persistent per-day caps, min-interval, block detector, exponential backoff,
circuit breakers (gmaps blocks / send bounces), Nominatim bbox cache, Overpass/job-feed backoff, send
drip jitter (initial **and** follow-up), domain-health → park-send, shared send budget so send+followup
can't jointly overshoot, systemd non-overlap.

Still your responsibility: (1) a **residential proxy** for bot-gmaps — without it Maps CAPTCHAs fast;
(2) keeping the **server clock/timezone** sane; (3) the eventual **custom sending domain** (DEPLOYMENT_PLAN
§6) — consumer Gmail caps your ceiling and one misstep can suspend the whole account; (4) re-tuning the
Maps **extraction selectors** when Google changes its markup.
