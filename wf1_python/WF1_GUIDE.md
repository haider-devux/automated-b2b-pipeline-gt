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
| `db.py` | insert company/lead, dedup by domain, discovery_cells accounting, audit log |
| `wf1.py` | reads the CSV, normalizes rows, inserts DISCOVERED |
| `sample_leads.csv` | a ready-to-run example (6 businesses across 6 cells) |

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

## Human-in-the-loop review queue (the main discovery path now)
Per the blueprint ("bots discover, humans act"), finds land in a `discovery_candidates` staging
table and a human approves them before they enter the pipeline — junk never touches real leads.

- **Setup once:** `..\wf3_python\.venv\Scripts\python.exe init_db.py` (creates `discovery_candidates`).
- **Use it:** open the dashboard → **Review** tab (http://localhost:5000/review).
  - **Add a company** you found on LinkedIn / Upwork / Fiverr / Google Maps / anywhere (you browse
    those normally; the bot never touches them — that's the compliant "human touch").
  - **Approve** → it becomes a `DISCOVERED` lead and flows into WF-2/WF-3. **Reject** → dropped.
- Free auto-collectors (below) will feed this same queue by inserting rows with `source='osm'` etc.

## Free "real" discovery sources to add later (no paid keys)
Each would be a new importer calling the same `db.insert_company` / `db.insert_lead`:
- **OpenStreetMap Overpass API** — businesses by city + category (no key, no card). Replaces paid Google Maps for discovery.
- **Job-board feeds for hiring intent** (Segment B signal): Greenhouse & Lever public JSON boards,
  RemoteOK API, Hacker News "who's hiring". All free.

## Note on the current WF-2
WF-2 is in **mock mode** and only recognizes its own demo domains, so CSV-imported leads won't
auto-enrich yet. The next step is a **free-enrichment mode** for WF-2 (dnspython MX/email check +
tech detection from the site + Lighthouse) so these real/free leads flow all the way through.
