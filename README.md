# Granjur B2B Cold-Outreach Pipeline

100% free, self-hosted cold-email automation. It **discovers** businesses (OpenStreetMap, job boards,
Google Maps, CSV), **enriches** them with a public email + tech signals, **qualifies** and writes a
personalised pitch with a **local LLM**, then **sends** from Gmail with an automated **follow-up drip** —
reply detection, deliverability warmup, and a web dashboard. No paid APIs.

Runs unattended on a Linux server as a **fleet of scheduled bots**, each rate-governed so nothing gets
flagged or blacklisted.

**Every run produces** (in `exports/`):
- `discovered.xlsx` — raw lake: every company found, all fields + a By-Source sheet.
- `granjur_central.xlsx` — pipeline CRM: each company's status journey (discovered → enriched → queued → contacted → replied).

---

## Requirements
- **Python 3.11+**, **PostgreSQL 14+** (with `citext` + `pgcrypto` extensions)
- **[Ollama](https://ollama.com)** with the `qwen2.5:3b` model (writes the pitch, locally)
- A **Gmail account** + an **[App Password](https://support.google.com/accounts/answer/185833)** (SMTP send + IMAP reply scan)
- Optional extras (PageSpeed key, cloudflared tunnel, Google-Maps proxy) — see [`deploy/README.md`](deploy/README.md)

---

## Deploy to a Linux server

> **This is the quick runbook.** For the full reference — every bot's schedule, all env knobs, the
> Google-Maps proving steps, and troubleshooting — see **[`deploy/README.md`](deploy/README.md)**.

Do these **in order**. It's safe by default (dry-run) until step 7.

**1 — On your machine: push the code**
```bash
<<<<<<< Updated upstream
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
=======
git add -A && git commit -m "deploy" && git push
>>>>>>> Stashed changes
```

**2 — On the server: get the code + dependencies**
```bash
sudo useradd -r -m -d /opt/granjur granjur
sudo -u granjur git clone https://github.com/haider-devux/automated-b2b-pipeline-gt /opt/granjur
cd /opt/granjur
sudo -u granjur python3 -m venv .venv
sudo -u granjur .venv/bin/pip install -r requirements.txt
```

**3 — Create the database + load the schema**
```bash
createdb granjur_pipeline
psql -d granjur_pipeline -c "CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS pgcrypto;"
for f in DB phase1_maxplan_migration phase3_schema_migration phase6_analytics_migration phase7_governor_migration; do
  psql -d granjur_pipeline -f database/$f.sql
done
ollama pull qwen2.5:3b
```

**4 — Configure `.env`** in `/opt/granjur` (`chmod 600`). Start in **safe dry-run**:
```ini
DB_HOST=localhost
DB_NAME=granjur_pipeline
DB_USER=postgres
DB_PASSWORD=your_db_password
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=your_16_char_app_password
IMAP_HOST=imap.gmail.com
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=qwen2.5:3b
GRANJUR_DRY_RUN=1          # 1 = safe (no email sent). Flip to 0 to go live (step 7).
GRANJUR_DAILY_TARGET=19    # send-ready leads per day
```
```bash
sudo timedatectl set-timezone UTC
```

**5 — Install the bots**
```bash
sudo bash deploy/install.sh
```
The **11 core bots** start immediately in dry-run (discover → enrich → qualify, **no email sent**).
`bot-gmaps` installs **disabled** (step 8).

**6 — Watch 2–3 days in dry-run** — confirm data looks right and caps/rests behave:
```bash
journalctl -u 'granjur@*' --since today
/opt/granjur/.venv/bin/python scripts/governor.py     # per-bot caps / rests
```

**7 — Go live: flip `1 → 0`** in `.env`:
```ini
GRANJUR_DRY_RUN=0         # real emails now send — dripped + warmup-capped
```
> `GRANJUR_DRY_RUN=1` = safe (no sends), `=0` = live. **Start at 1, flip to 0.** Never the reverse.

**8 — (optional) Enable the Google Maps bot** — experimental, off by default. Prove it by hand first
(needs `playwright install chromium` + a residential proxy in `GMAPS_PROXIES`), then:
```bash
sudo systemctl enable --now granjur-gmaps.timer       # kill switch: disable --now
```

**Full per-bot schedule, all env knobs, troubleshooting → [`deploy/README.md`](deploy/README.md).**

---

## Why it won't get flagged / blacklisted
Every bot is rate-governed (`scripts/governor.py`): per-source **daily caps**, a **stop-and-rest clock**,
exponential backoff, and **circuit breakers**. Sending **drips 45–120s apart** under a **warmup cap**, with
a **bounce breaker** and per-recipient **local-time windows**. A blocked bot rests without touching the
others. Full design + per-bot specs → [`DEPLOYMENT_PLAN.md`](DEPLOYMENT_PLAN.md).

---

## How it works
Four decoupled phases, communicating only through the lead's `status` in PostgreSQL:

| Phase | Folder | Does |
|-------|--------|------|
| **WF-1 Discovery** | `wf1_python/` | OSM + job boards + Google Maps + CSV lane; dedup by domain |
| **WF-2 Enrichment** | `wf2_python/` | find a public email (deep site scrape) + tech; MX-verify |
| **WF-3 Qualify** | `wf3_python/` | segment + write the pitch (local LLM); host the dashboard |
| **WF-4 Outreach** | `wf4_python/` | send via Gmail, follow-up drip, reply detection, warmup |

Orchestration + shared tools are in `scripts/` (`run_pipeline.py`, `governor.py`, `export_leads_csv.py`);
schema + migrations in `database/`.

---

## Run locally (dev)
```bash
python scripts/run_pipeline.py                       # whole pipeline once (dry-run by default)
python scripts/run_pipeline.py --test you@gmail.com  # preview pitches to your OWN inbox (DB untouched)
cd wf3_python && ../.venv/bin/python dashboard.py     # dashboard at http://localhost:5000
```
