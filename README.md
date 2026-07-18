# Granjur B2B Cold-Outreach Pipeline

A 100% free, self-hosted cold-email automation pipeline. It **discovers** local businesses from
OpenStreetMap, **enriches** them with a public email + tech signals, **qualifies** them and writes a
personalised pitch with a **local LLM**, then **sends** from a single Gmail account and runs an automated
**follow-up drip** — with reply detection, email threading, deliverability warmup, and a web dashboard.

No paid APIs. Everything runs on your own machine: Python + PostgreSQL + a local Ollama model + Gmail.

---

## Requirements

**System**
- **Python 3.13** (3.11+ should work)
- **PostgreSQL 14+** — with the `citext` and `pgcrypto` extensions available
- **[Ollama](https://ollama.com)** running locally with the **`qwen2.5:3b`** model (used only to write/translate the pitch)
- A **Gmail account** with an **[App Password](https://support.google.com/accounts/answer/185833)** (used for SMTP send + IMAP reply scanning)

**Optional**
- A free **[PageSpeed Insights API key](https://developers.google.com/speed/docs/insights/v5/get-started)** (adds a live site-speed link to pitches)
- **[cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)** (exposes the open/click tracking server via a free public tunnel)
- **pandas** (faster bulk registry/quota CSV import; falls back to the stdlib `csv` module if absent)

**Python packages** — see [`requirements.txt`](requirements.txt) (Flask, psycopg2, requests, dnspython,
python-dotenv, openpyxl, python-docx, tzdata).

---

## Setup

```bash
# 1. Clone
git clone https://github.com/haider-devux/automated-b2b-pipeline-gt
cd "B2B Pipeline"

# 2. Create a virtual environment + install dependencies
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
python -m pip install -r requirements.txt

# 3. Create the database and load the schema
createdb granjur_pipeline
psql -d granjur_pipeline -c "CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS pgcrypto;"
psql -d granjur_pipeline -f database/DB.sql
psql -d granjur_pipeline -f database/phase1_maxplan_migration.sql
psql -d granjur_pipeline -f database/phase3_schema_migration.sql
psql -d granjur_pipeline -f database/phase6_analytics_migration.sql

# 4. Pull the local LLM
ollama pull qwen2.5:3b

# 5. Configure your secrets (see the next section), then run
python scripts/run_pipeline.py
```

---

## Configuration (`.env`)

Create a `.env` file in the project root. **Never commit real secrets.**

```ini
# --- PostgreSQL ---
DB_HOST=localhost
DB_PORT=5432
DB_NAME=granjur_pipeline
DB_USER=postgres
DB_PASSWORD=your_db_password

# --- Gmail (SMTP send + IMAP reply scan) ---
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=your_16_char_app_password
IMAP_HOST=imap.gmail.com

# --- Local LLM (Ollama) ---
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=qwen2.5:3b

# --- Pipeline behaviour ---
GRANJUR_DRY_RUN=1            # 1 = safe dry-run (nothing sent); set 0 to send for real
GRANJUR_DAILY_TARGET=19      # send-ready leads to line up per day
GRANJUR_BOOKING_LINK=        # your Google Calendar / Cal.com booking URL (pitch CTA)

# --- Optional ---
PAGESPEED_API_KEY=           # free Google PageSpeed key (site-speed link in pitches)
GRANJUR_TRACK_BASE=          # public URL of the tracking server (via cloudflared) for open/click stats
GRANJUR_REGION_TEST_INBOX=   # route dashboard "Send" buttons to your own inbox for testing
```

Sender identity (`GRANJUR_SENDER_NAME`, `GRANJUR_SENDER_TITLE`, `GRANJUR_PHONE`, `GRANJUR_WEBSITE`,
`GRANJUR_TAGLINE`, `GRANJUR_PRIVACY_URL`) and tuning knobs (`GRANJUR_PER_CELL`, `GRANJUR_FOLLOWUP_DAYS`,
`GRANJUR_WARMUP_CEIL`) are also read from the environment — all have sensible defaults.

---

## Usage

```bash
# One command runs the whole pipeline (discover -> enrich -> qualify -> send -> follow-up -> export).
# SAFE by default: dry-run, nothing is actually emailed.
python scripts/run_pipeline.py

# Preview: send every queued pitch to your OWN inbox (DB untouched)
python scripts/run_pipeline.py --test you@gmail.com

# Go live (real sends via Gmail, capped by the mailbox warmup safety)
#   PowerShell:  $env:GRANJUR_DRY_RUN = "0"; python scripts/run_pipeline.py
#   bash:        GRANJUR_DRY_RUN=0 python scripts/run_pipeline.py

# Isolate one market, or stop before sending
python scripts/run_pipeline.py --region GCC
python scripts/run_pipeline.py --no-send
```

**Dashboard** (leads, outreach, analytics, replies, follow-ups, deliverability health):

```bash
cd wf3_python
..\.venv\Scripts\python.exe dashboard.py    # then open http://localhost:5000
```

---

## How it works

The pipeline is four decoupled phases that communicate only through the lead's `status` in PostgreSQL:

| Phase | Folder | Does |
|-------|--------|------|
| **WF-1 Discovery** | `wf1_python/` | CSV import + free collectors (OpenStreetMap, job boards), dedup by domain |
| **WF-2 Enrichment** | `wf2_python/` | Find a public email (deep site scraping) + tech stack; MX-verify addresses |
| **WF-3 Qualify + Pitch** | `wf3_python/` | Segment the lead, write the pitch with the local LLM, host the dashboard |
| **WF-4 Outreach** | `wf4_python/` | Send via Gmail, threaded follow-up drip, reply detection, warmup safety |

Orchestration + shared tools live in `scripts/` (`run_pipeline.py`, `export_leads_csv.py`, quota/registry
importers, tracking server). Database schema + migrations are in `database/`.

---

## Output

All exports are Excel (`.xlsx`, opens in Excel 2010+), written to `exports/`:

- **`granjur_central.xlsx`** — the one central database. Sheets: **Summary** (current counts),
  **Runs Log** (one row per run over time), **Latest Leads** (full live list).
- **`granjur_report_<timestamp>.xlsx`** — a frozen per-run snapshot (Summary + Companies). The folder
  auto-prunes to the most recent few.

The dashboard's **Analytics → Download central Excel database** button serves the same central file, live.

---

## Safety notes

- **Dry-run by default** — real sending is a deliberate `GRANJUR_DRY_RUN=0` switch.
- All live sends stay within a **mailbox warmup cap** to protect Gmail deliverability.
- The LLM runs **locally** — company data never leaves your machine.
- Unsubscribe/bounce suppression and reply detection stop contacting anyone who opts out or replies.
