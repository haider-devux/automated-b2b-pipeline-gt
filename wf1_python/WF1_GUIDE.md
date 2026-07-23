# WF-1 (Python) — Discovery Ingestion (CSV importer)

> First phase of the pipeline. Turns a list of businesses into `DISCOVERED` leads that WF-2
> then enriches. This is the **always-free** discovery path. Integrates with the rest of the
> pipeline **only** through the `leads.status` column.

---

## Where it fits — the relay race
```
CSV / free sources  →  DISCOVERED (WF-1 = this)  →  ENRICHED (WF-2)  →  QUALIFIED (WF-3)  →  outreach (WF-4)
```

## What it does
- Reads a CSV of businesses.
- Inserts each as a `companies` row + a `DISCOVERED` `leads` row.
- **Dedups on domain** — re-running never creates duplicate leads (blueprint idempotency rule).
- Updates **`discovery_cells`** depletion accounting per `(region, city, niche)` so we can later
  see when a city×niche is exhausted and rotate instead of re-scraping the same list.

## Files
| File | Does |
|---|---|
| `config.py` | reuses WF-3's DB password; valid region codes; depletion threshold |
| `db.py` | insert company/lead, dedup by domain, discovery_cells accounting, `city_bbox` geocode cache, audit log |
| `wf1.py` | reads a CSV, normalizes rows, inserts DISCOVERED (the LinkedIn/Upwork/Fiverr lane) |
| `intake.py` | auto-evaluate each candidate vs `targets.py` → APPROVE / REJECT / PENDING |
| `targets.py` | who we want: regions, employee range, chain blocklist, per-region city pools |
| `collect_osm.py` | **bot-osm** — OpenStreetMap (Nominatim→Overpass), bbox-cached, governed |
| `collect_jobs.py` | **bot-remoteok / bot-remotive** — free job feeds (hiring intent), governed |
| `collect_maps.py` | **bot-gmaps** — Google Maps scrape behind the 5-layer safety trigger (Playwright + proxy) |
| `sample_leads.csv` | a ready-to-run example (6 businesses across 6 cells) |

> All collectors call the shared **`scripts/governor.py`** before working (per-source daily caps + a
> `rest_until` backoff on rate-limits/blocks) so a scheduled loop can never get a source flagged/banned.
> See `DEPLOYMENT_PLAN.md` for the full bot fleet + per-source anti-flag design.

## Run it (uses the WF-3 venv)
From this folder (`wf1_python`):
```powershell
..\wf3_python\.venv\Scripts\python.exe wf1.py                 # imports sample_leads.csv
..\wf3_python\.venv\Scripts\python.exe wf1.py path\to\your.csv # your own file
```
Then watch the dashboard (http://localhost:5000) — new `DISCOVERED` leads appear in the funnel.

## CSV format
Required: `legal_name`, `region`, `city`, `niche`. Everything else is optional.
Full columns:
```
legal_name, domain, region, country, city, niche,
website_url, phone, employee_count, tech_stack, first_name, last_name, job_title, email
```
- `region` must be one of: `US, EU, UK, GCC, CN, AU, OTHER` (anything else becomes `OTHER`).
- `tech_stack` is semicolon-separated: `React;Node;AWS`.
- **Free enrichment tip:** if you already know `employee_count`, `tech_stack`, or the contact's
  `email`, put them in the CSV. Then WF-2 only has to fill/verify the gaps.

## Auto-intake (hands-off — "bots discover, humans act on the pitch")
Every find (collector or CSV) funnels through `intake.py` into the `discovery_candidates` staging table
and is auto-evaluated against `targets.py`: meets the rules → auto-APPROVE (becomes a `DISCOVERED` lead
and flows into WF-2/WF-3); clearly fails → auto-REJECT; genuinely ambiguous → PENDING. The human reviews
the **pitch** before outreach, not every company. (The old manual "Add/Review" dashboard tab was removed —
discovery is automation-only now.)

- **Setup once:** `..\wf3_python\.venv\Scripts\python.exe init_db.py` (creates `discovery_candidates`).

## Discovery sources (all implemented, all free unless noted)
Each collector calls `db.insert_company` / `db.insert_lead` via `intake.submit()` and is governed by
`scripts/governor.py`:
- **OpenStreetMap** (`collect_osm.py`, bot-osm) — businesses by city + category via Nominatim→Overpass
  (no key, no card). The free replacement for paid Google Maps. Geocodes cached in `city_bbox`.
- **Job boards** (`collect_jobs.py`, bot-remoteok / bot-remotive) — RemoteOK + Remotive hiring-intent feeds.
- **Google Maps** (`collect_maps.py`, bot-gmaps) — direct scrape behind the 5-layer safety trigger; needs
  Playwright + a residential proxy (`GMAPS_PROXIES`) and stays inert otherwise. See `DEPLOYMENT_PLAN.md §4.1`.
- **LinkedIn / Fiverr / Upwork** — CSV lane only (ban risk): drop a CSV in `inbox/` (bot-csv) or run
  `wf1.py <csv>`. The server never scrapes those platforms.

## WF-2 enrichment
WF-2 runs in **free mode by default** (`config.ENRICH_MODE=free`): dnspython MX/email check + site tech
detection + deep Contact/About email scraping (+ optional Lighthouse). CSV-imported and collector leads
flow all the way through — no paid keys, no mock domains required.
